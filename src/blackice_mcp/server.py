from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import __commit__, __version__
from .agent import AgentConnection
from .apidocs import ApiDocs
from .build import run_dotnet_build, stage_bundle
from .hub import DEAD, Hub
from .paths import IS_LINUX, IS_WINDOWS, default_client_install, default_profiles_path
from .protocol import AgentError, StreamResult
from .supervisor import (
    CLIENT,
    KINDS,
    NATIVE_SETUP,
    Profile,
    Supervisor,
    UGC_SETUP,
    parse_profile,
    save_profile,
)

BUNDLE_POLL_INTERVAL_S = 0.25
BUNDLE_RESTART_TIMEOUT_MS = 15000

WAIT_POLL_INTERVAL_S = 0.25
WAIT_PING_TIMEOUT_S = 2.0
SHUTDOWN_REQUEST_TIMEOUT_S = 2.0

# The transport caps a request at 30s, so anything that waits has to answer inside that.
WAIT_FOR_MAX_TIMEOUT_S = 20.0

CONNECTED = "connected"
JOINING_CLIENT_PHASES = ("initializing", "connecting")
# BlackICE does not allow joining again after a session ended. The client needs to be replaced
TERMINAL_CLIENT_PHASES = ("disconnected", "failed")

LINUX_CLIENT_LAUNCHER = (
    "A BlackICE client is a Windows binary, so on Linux it needs a compatibility layer. Ask the "
    "user how they run it - typically Proton through Steam, or wine - and set the profile's "
    "'launcher' to that command, for example ['proton', 'run'] or ['wine']. Without it, "
    "spawn_client fails."
)


def _describe(agent: AgentConnection, liveness: str | None = None) -> dict[str, Any]:
    state = agent.hello.state
    status = agent.status
    payload = {
        "realm": state.realm,
        "slot": state.slot,
        "pid": state.pid,
        "workdir": state.workdir,
        "build_commit": state.build_commit,
        "runtime_stage": state.runtime_stage,
        "logfile": state.logfile,
        "phase": status.phase,
        "phase_detail": status.detail,
        "connected": not agent.closed,
    }
    if liveness is not None:
        payload["liveness"] = liveness
    return payload


def _stream_payload(result: StreamResult) -> dict[str, Any]:
    return {
        "cursor": result.cursor,
        "dropped": result.dropped,
        "lines": [
            {"seq": line.seq, "source": line.source, "module": line.module, "text": line.text}
            for line in result.lines
        ],
    }


# A page's console line: [pid:tid:MMDD/HHMMSS.mmm:LEVEL:CONSOLE:line] "text", source: url (line)
CONSOLE_LINE = re.compile(
    r"^\[\d+:\d+:\d{4}/\d{6}\.\d+:(?P<level>[A-Z]+):CONSOLE(?::\d+|\s*\(\d+\))?\] (?P<body>.*)$"
)


def browser_logfile(main: Path) -> Path:
    """The browser's own log for a process, which sits beside its main one."""
    return main.with_name(f"{main.stem}_cef{main.suffix}")


def console_messages(lines: list[str]) -> list[str]:
    """The page-facing lines of a browser log: console output, exceptions and policy messages."""
    found: list[str] = []

    for line in lines:
        match = CONSOLE_LINE.match(line)
        if match is not None:
            found.append(f"{match['level']} {match['body']}")

    return found


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _is_terminal_client(agent: AgentConnection) -> bool:
    return agent.hello.state.realm == CLIENT and agent.status.phase in TERMINAL_CLIENT_PHASES


def _replace_guidance(agent: AgentConnection, action: str) -> str:
    state = agent.hello.state

    return (
        f"{action} cannot use client:{state.slot}: it is {agent.status.phase}, and a client "
        "cannot return to the menu once it has connected, so it can only be stopped. Replace it: "
        f"stop_process(pid={state.pid}), then spawn_client, then connect_client."
    )


def _connect_refusal(agent: AgentConnection) -> str | None:
    state = agent.hello.state

    if agent.status.phase in JOINING_CLIENT_PHASES:
        return (
            f"client:{state.slot} is {agent.status.phase}; wait for it to reach 'menu' before "
            "connecting it (list_processes shows its current phase)."
        )

    if agent.status.phase != "menu":
        return _replace_guidance(agent, "connect")

    return None


def _normalise_bindaddr(bindaddr: str) -> str | None:
    port = ""

    if bindaddr.startswith("["):
        close = bindaddr.find("]")
        if close == -1:
            return None
        host = bindaddr[1:close]
        remainder = bindaddr[close + 1 :]
        port = remainder[1:] if remainder.startswith(":") else ""
    else:
        host, _, port = bindaddr.rpartition(":")

    if not port.isdigit():
        return None

    if host in ("", "::", "0.0.0.0", "*"):
        host = "127.0.0.1"

    if ":" in host:
        return f"[{host}]:{port}"

    return f"{host}:{port}"


class McpSurface:
    def __init__(
        self,
        hub: Hub,
        supervisor: Supervisor,
        profiles: dict[str, Profile] | None = None,
        api_docs: ApiDocs | None = None,
        profiles_path: Path | None = None,
        setup_type: str = UGC_SETUP,
    ) -> None:
        self._hub = hub
        self._supervisor = supervisor
        self._profiles = profiles or {}
        self._api_docs = api_docs or ApiDocs()
        self._profiles_path = profiles_path or default_profiles_path()
        self._setup_type = setup_type

        self._supervisor.request_shutdown = self._request_shutdown

        vs_note = ""

        if setup_type == NATIVE_SETUP:
            vs_note = (
                "On Windows the vs_ tools drive the user's open Visual Studio 2022: vs_build builds "
                "there, vs_attach attaches its debugger to a hub process, and vs_breakpoint_add, "
                "vs_control, vs_state and vs_eval debug it. The user sees all of it in their IDE "
                "and may be working in it, so do not leave the debugger stopped or detach it "
                "without saying so.\n"
                "\n"
            )
        self._server = MCPServer(
            name="blackice-mcp",
            version=__version__,
            instructions=(
                "Drive BlackICE / REServer development.\n"
                "\n"
                "Processes reach this hub by being started with --mcp; list_processes shows which "
                "are connected, and list_profiles shows what can be spawned. Tools take an "
                "optional 'target' (pid, realm like 'server', or realm:slot like 'client:0'); "
                "with one process connected it can be omitted.\n"
                "\n"
                "If list_profiles is empty, the user has not told the hub where their installs "
                "live yet. Ask them for the server install directory (the one containing REServer) "
                "and, if they want to launch clients, the client install directory, then record "
                "each with configure_profile. Do not guess a path. If list_profiles reports a "
                "detected_client_install, offer that path rather than asking the user to type it - "
                "it is where the launcher puts a client when they did not choose portable.\n"
                "\n"
                "Check hub_info for the platform before planning to launch a client. On Linux a "
                "client is a Windows binary and needs a compatibility layer, so ask the user how "
                "they run it and set the profile's launcher accordingly; hub_info explains this. "
                "On Windows no launcher is needed.\n"
                "\n"
                "Driving a process: console_exec runs a console command and returns the output it "
                "produced. The console holds every convar, every built-in command and every "
                "command a bundle registered, so convars, bundle control and UGC commands are all "
                "console_exec. stream_read returns console output and log lines after a cursor; "
                "both sources share one sequence, so pass the returned cursor back to resume "
                "without missing or repeating lines.\n"
                "\n"
                "The dev loop is build_bundle -> deploy_bundle -> bundle_restart. build_bundle "
                "runs a plain 'dotnet build' and cannot know about extra steps a bundle needs (a "
                "web UI, generated assets); run those yourself and check deploy_bundle's 'copied' "
                "list to confirm what was staged. bundle_restart waits for the server to report "
                "the bundle started, so 'confirmed: false' means it did not - read the returned "
                "lines for the reason rather than retrying.\n"
                "\n"
                "Before writing bundle code, read the authoring guide at "
                "https://docs.replay.re/ugc/authoring/ - it covers the project layout, the build "
                "and deploy flow, bundle config, the web UI and known pitfalls. Then use "
                "search_api and describe_api to find the exact API and how to call it. "
                "Signatures are the guest-facing ones; a method that lists ulong or byte[] in its "
                "structured parameters takes a string or a generic in the guest surface, so trust "
                "the signature. Members declared on a base class are marked 'inherited_from': the "
                "game API puts shared behaviour high in the hierarchy, so if a type seems to lack "
                "a method, look at what it inherits before concluding it does not exist.\n"
                "\n"
                "\n"
                "On Windows, screenshot captures a client's game window as the user sees it. To "
                "inspect the web UI's DOM, use the client's Chrome DevTools endpoint on "
                "127.0.0.1:(2077 + process slot), which is standard CDP - point a "
                "chrome-devtools MCP server at it."
                + vs_note
            ),
        )
        self._register()

    @property
    def server(self) -> MCPServer:
        return self._server

    async def run_stdio(self) -> None:
        await self._server.run_stdio_async()

    def _register(self) -> None:
        server = self._server

        @server.tool(
            name="hub_info",
            description=(
                "Which hub this is: version, pid, endpoint, docs URL and what it can spawn. "
                "Use it to tell one hub from another, or to report a version when something "
                "behaves unexpectedly."
            ),
        )
        def hub_info() -> str:
            host, port = self._hub.endpoint
            platform_name = "windows" if IS_WINDOWS else ("linux" if IS_LINUX else sys.platform)

            payload: dict[str, Any] = {
                "name": "blackice-mcp",
                "version": __version__,
                "commit": __commit__,
                "pid": os.getpid(),
                "endpoint": f"{host}:{port}",
                "api_docs_url": self._api_docs.base_url,
                "platform": platform_name,
                "profiles": sorted(self._profiles),
                "processes_connected": len(self._hub.agents()),
            }

            if IS_LINUX:
                payload["client_spawning"] = (
                    f"REServer runs natively here. {LINUX_CLIENT_LAUNCHER} The hub cannot start a "
                    "client from a Steam shortcut, since that goes through Steam's own environment."
                )
            elif IS_WINDOWS:
                payload["client_spawning"] = (
                    "This hub is on Windows, so a client launches directly and no launcher is "
                    "needed."
                )

            return json.dumps(payload)

        @server.tool(
            name="list_processes",
            description="List every BlackICE/REServer process currently connected to the hub.",
        )
        def list_processes() -> dict[str, Any]:
            live = [_describe(agent, self._hub.liveness(agent)) for agent in self._hub.agents()]
            return {"processes": live, "dead": self._hub.tombstones()}

        @server.tool(
            name="process_state",
            description="Read one process's identity: realm, slot, pid, workdir, build commit.",
        )
        def process_state(target: str | None = None) -> str:
            return self._guard(lambda: json.dumps(self._hub.find(target).call("state.get")))

        @server.tool(
            name="console_exec",
            description=(
                "Run a console command on a process and return the output it produced. This is the "
                "console, so convars, bundle commands and UGC-registered commands all work."
            ),
        )
        def console_exec(command: str, target: str | None = None, wait_ms: int | None = None) -> str:
            def run() -> str:
                agent = self._hub.find(target)

                if _is_terminal_client(agent):
                    return _error(_replace_guidance(agent, "console_exec"))

                params: dict[str, Any] = {"command": command}
                if wait_ms is not None:
                    params["wait_ms"] = wait_ms
                result = StreamResult.from_wire(agent.call("console.exec", params))
                return json.dumps(_stream_payload(result))

            return self._guard(run)

        @server.tool(
            name="stream_read",
            description=(
                "Read console output and log lines after a cursor. Both sources share one sequence, "
                "so lines come back interleaved in true order."
            ),
        )
        def stream_read(
            cursor: int = 0,
            target: str | None = None,
            source: str = "all",
        ) -> str:
            def run() -> str:
                agent = self._hub.find(target)
                result = agent.drain_stream(cursor)

                if source in ("log", "console"):
                    result = StreamResult(
                        cursor=result.cursor,
                        dropped=result.dropped,
                        lines=[line for line in result.lines if line.source == source],
                    )

                return json.dumps(_stream_payload(result))

            return self._guard(run)

        @server.tool(
            name="bundle_list",
            description=(
                "List the UGC bundles the server has discovered, split into those currently "
                "running and those only available to start. A discovered bundle is not a running "
                "one: use bundle_start to run it. Each row also marks whether the bundle carries "
                "a script blob for the client and server realm."
            ),
        )
        def bundle_list(target: str | None = None) -> str:
            def run() -> str:
                agent = self._hub.find(target)
                return json.dumps(_stream_payload(self._exec(agent, "bundles", 1500)))

            return self._guard(run)

        @server.tool(
            name="bundle_start",
            description="Start a UGC bundle on the server.",
        )
        def bundle_start(name: str, target: str | None = None) -> str:
            return self._guard(
                lambda: json.dumps(self._bundle(self._hub.find(target), name, "start"))
            )

        @server.tool(
            name="bundle_stop",
            description="Stop a running UGC bundle on the server.",
        )
        def bundle_stop(name: str, target: str | None = None) -> str:
            return self._guard(
                lambda: json.dumps(self._bundle(self._hub.find(target), name, "stop"))
            )

        @server.tool(
            name="search_api",
            description=(
                "Search the scripting API for the realm: classes, functions, properties and enums, "
                "with signatures. Use this to find what exists before writing bundle code. "
                "Matching is name-first, so a bare member name finds every declaration. Filter by "
                "kind or by class; the class filter follows base classes too. "
                "For prose, worked examples and known pitfalls, see the authoring guide at "
                "https://docs.replay.re/ugc/authoring/ - read it before writing a bundle."
            ),
        )
        def search_api(
            query: str = "",
            realm: str = "client",
            kind: str | None = None,
            class_name: str | None = None,
            limit: int = 40,
        ) -> str:
            def run() -> str:
                results = self._api_docs.search(
                    query=query,
                    realm=realm,
                    kind=kind,
                    class_name=class_name,
                    limit=limit,
                )
                return json.dumps([entry.to_dict() for entry in results])

            return self._guard(run)

        @server.tool(
            name="describe_api",
            description=(
                "Look up one API member and return its full signature, parameters and result. "
                "Accepts a qualified name (ent.Entity.GetWorldPosition), a bare name "
                "(GetWorldPosition), or the type you hold even when the member is inherited "
                "(game.Object.GetWorldPosition). Members declared on a base class are marked with "
                "'inherited_from' - the dumped game API puts shared behaviour high in the "
                "hierarchy, so a member missing from a type is usually reachable from its bases."
            ),
        )
        def describe_api(name: str, realm: str = "client") -> str:
            def run() -> str:
                results = self._api_docs.describe(name, realm=realm)
                if not results:
                    return _error(f"nothing matching '{name}' in realm '{realm}'")
                return json.dumps([entry.to_dict() for entry in results])

            return self._guard(run)

        @server.tool(
            name="class_members",
            description=(
                "List every function and property reachable from a class, including those it "
                "inherits. Inherited members carry 'inherited_from'; members declared directly on "
                "the class do not."
            ),
        )
        def class_members(class_name: str, realm: str = "client") -> str:
            def run() -> str:
                results = self._api_docs.members_of(class_name, realm=realm)
                if not results:
                    return _error(f"no class '{class_name}' in realm '{realm}'")
                return json.dumps([entry.to_dict() for entry in results])

            return self._guard(run)

        @server.tool(
            name="read_log",
            description=(
                "Read a process's log file, optionally just the tail. Prefer stream_read while a "
                "process is running; use this for a process that has died, since the file is the "
                "only record that survives a crash. A dead process stays listed under 'dead' in "
                "list_processes so it can still be read after the fact. 'source' chooses the file: "
                "'main' is the process's own log, 'console' is what its browser logged on behalf of "
                "pages - console output, exceptions and the browser's own policy messages, filtered "
                "out of a much noisier file - and 'cef' is that file unfiltered."
            ),
        )
        def read_log(
            target: str | None = None,
            tail: int = 200,
            pid: int | None = None,
            source: str = "main",
        ) -> str:
            def run() -> str:
                if source not in ("main", "console", "cef"):
                    return _error(f"source must be 'main', 'console' or 'cef', got '{source}'")

                path = self._logfile_for(target, pid)
                if path is None:
                    return _error(
                        "no log file known for that process. It may never have been connected, "
                        "or the hub did not learn its logfile."
                    )

                if source != "main":
                    path = browser_logfile(path)

                if not path.is_file():
                    return _error(f"log file does not exist: {path}")

                file_lines = path.read_text(errors="replace").splitlines()
                selected = console_messages(file_lines) if source == "console" else file_lines

                return json.dumps(
                    {
                        "path": str(path),
                        "source": source,
                        "total_lines": len(file_lines),
                        "matched": len(selected),
                        "lines": selected[-tail:] if tail > 0 else selected,
                    }
                )

            return self._guard(run)

        @server.tool(
            name="configure_profile",
            description=(
                "Record where a server or client install lives, so it can be spawned later. Use "
                "this when list_profiles is empty and the user has told you their paths. "
                "The install directory must already contain the binary. Replacing an existing "
                "profile requires overwrite=true."
            ),
        )
        def configure_profile(
            name: str,
            install_dir: str,
            kind: str,
            bindaddr: str | None = None,
            launcher: list[str] | None = None,
            overwrite: bool = False,
        ) -> str:
            def run() -> str:
                if kind not in KINDS:
                    return _error(f"kind must be one of {list(KINDS)}, got '{kind}'")

                try:
                    profile = parse_profile(
                        name,
                        {
                            "kind": kind,
                            "install_dir": install_dir,
                            "bindaddr": bindaddr,
                            "launcher": launcher or [],
                        },
                    )
                except ValueError as exc:
                    return _error(str(exc))

                binary = profile.binary()
                if not binary.is_file():
                    return _error(
                        f"no {binary.name} in '{profile.install_dir}'. "
                        "Point install_dir at the directory containing the binary."
                    )

                try:
                    save_profile(self._profiles_path, profile, overwrite=overwrite)
                except ValueError as exc:
                    return _error(str(exc))

                self._profiles[name] = profile

                result: dict[str, Any] = {
                    "saved": profile.to_dict(),
                    "path": str(self._profiles_path),
                }

                if IS_LINUX and kind == CLIENT and not profile.launcher:
                    result["warning"] = LINUX_CLIENT_LAUNCHER

                return json.dumps(result)

            return self._guard(run)

        @server.tool(
            name="list_profiles",
            description="List the configured process profiles this hub can spawn.",
        )
        def list_profiles() -> str:
            payload: dict[str, Any] = {
                "profiles": [profile.to_dict() for profile in self._profiles.values()],
            }

            detected = default_client_install()
            if detected is not None and (detected / "BlackICE.exe").is_file():
                payload["detected_client_install"] = str(detected)
                payload["detected_note"] = (
                    "A client install was found at the launcher's default location. Offer this "
                    "path when asking the user, rather than asking them to type it."
                )

            return json.dumps(payload)

        @server.tool(
            name="list_spawned",
            description=(
                "List the processes this hub started, and whether they have connected back. "
                "'target' is the value to pass to tools that take one: the process's realm:slot "
                "once it has connected, or its pid until then."
            ),
        )
        def list_spawned() -> str:
            targets = {
                agent.hello.state.pid: f"{agent.hello.state.realm}:{agent.hello.state.slot}"
                for agent in self._hub.agents()
            }
            return json.dumps(self._supervisor.list_spawned(targets))

        @server.tool(
            name="wait_for",
            description=(
                "Block until a process reaches a phase and return its state, instead of sleeping. "
                "'target' is a pid (straight after a spawn, before it has connected) or a "
                "realm:slot. 'phase' is 'connected' (it reached the hub) or a phase such as 'menu' "
                "or 'session_active'; comma-separate several to accept any of them. Returns as "
                "soon as the phase is reached, and early with an error if the process exits, its "
                "connection drops, or a client ends up disconnected or failed. The wait is capped "
                "at 20s to stay inside the transport's request timeout, so call again to keep "
                "waiting - each call re-checks the phase immediately."
            ),
        )
        def wait_for(target: str, phase: str = CONNECTED, timeout_s: float = WAIT_FOR_MAX_TIMEOUT_S) -> str:
            wanted = {part.strip() for part in phase.split(",") if part.strip()}
            return self._guard(lambda: self._wait_for(target, wanted, timeout_s))

        @server.tool(
            name="spawn_server",
            description="Start a REServer that connects back to this hub.",
        )
        def spawn_server(profile: str) -> str:
            def run() -> str:
                resolved = self._profile(profile)
                self._hub.check_server_spawn_allowed()
                spawned = self._supervisor.spawn_server(resolved)
                return json.dumps(spawned.to_dict(connected=False))

            return self._guard(run)

        @server.tool(
            name="spawn_client",
            description=(
                "Start a BlackICE client that connects back to this hub. On Linux the profile needs "
                "a 'launcher' (see hub_info). The client "
                "claims its own process slot from shared memory, so it cannot be chosen here."
            ),
        )
        def spawn_client(profile: str) -> str:
            def run() -> str:
                spawned = self._supervisor.spawn_client(self._profile(profile))
                return json.dumps(spawned.to_dict(connected=False))

            return self._guard(run)

        @server.tool(
            name="connect_client",
            description=(
                "Connect a client to a server. Pass the profile name of a server this hub spawned "
                "and the hub resolves it to the actual address; the client is never pointed at an "
                "arbitrary address by accident."
            ),
        )
        def connect_client(target: str | None = None, server_profile: str | None = None) -> str:
            def run() -> str:
                address = self._resolve_address(server_profile)
                if address is None:
                    if not self._hub.servers():
                        return _error(
                            "no server is connected, so there is no address to connect to; "
                            "spawn_server first"
                        )

                    return _error(
                        f"'{server_profile}' has no known address: the connected server did not "
                        "report one and the profile sets no bindaddr. A server spawned by a "
                        "previous hub is not recognised, so restart it so this hub owns it."
                    )

                agent = self._hub.find(target)
                if agent.hello.state.realm != "client":
                    return _error(
                        f"'connect' is a client command; target {agent.hello.state.realm}:"
                        f"{agent.hello.state.slot} is a {agent.hello.state.realm}. "
                        "Pass target='client' or 'client:<slot>'."
                    )

                refusal = _connect_refusal(agent)
                if refusal is not None:
                    return _error(refusal)

                return json.dumps(
                    _stream_payload(self._exec(agent, f"connect {address}", 1500))
                )

            return self._guard(run)

        @server.tool(
            name="stop_process",
            description=(
                "Stop a process this hub started. Processes you started yourself cannot be stopped "
                "here, only driven."
            ),
        )
        def stop_process(pid: int) -> str:
            def run() -> str:
                return json.dumps(self._supervisor.stop(pid))

            return self._guard(run)

        @server.tool(
            name="build_bundle",
            description=(
                "Runs 'dotnet build' in a bundle project directory. This builds only what the "
                "project's MSBuild targets produce: the wasm blobs, plus whatever the project's "
                "Directory.Build.targets stages. If the bundle has additional build steps (a web "
                "UI, generated assets, a custom toolchain), you must run those yourself - this "
                "tool cannot know about them. Check deploy_bundle's 'copied' list to confirm what "
                "actually got staged."
            ),
        )
        def build_bundle(project_dir: str, config: str = "Release") -> str:
            def run() -> str:
                result = run_dotnet_build(Path(project_dir), config)
                return json.dumps(result.to_dict())

            return self._guard(run)

        @server.tool(
            name="deploy_bundle",
            description=(
                "Stage a built bundle into a server's data/ugc/<name> so it can be started. "
                "Copies the whole source directory - the wasm blobs, config.toml, the web UI and "
                "any data files the bundle ships. Entries named by the bundle's [ReUi] files list "
                "that did not land on disk are reported in 'missing'."
            ),
        )
        def deploy_bundle(source_dir: str, server_dir: str, name: str) -> str:
            def run() -> str:
                result = stage_bundle(Path(source_dir), Path(server_dir), name)
                return json.dumps(result.to_dict())

            return self._guard(run)

        @server.tool(
            name="bundle_restart",
            description=(
                "Restart a UGC bundle on the server, re-reading it from disk first. Waits for the "
                "server to report the bundle started."
            ),
        )
        def bundle_restart(
            name: str,
            target: str | None = None,
            timeout_ms: int = BUNDLE_RESTART_TIMEOUT_MS,
        ) -> str:
            return self._guard(
                lambda: json.dumps(
                    self._bundle(self._hub.find(target), name, "restart", timeout=timeout_ms / 1000)
                )
            )

        if IS_WINDOWS:
            from . import screenshot as capture_window

            @server.tool(
                name="screenshot",
                description=(
                    "Capture a process's game window as a PNG: the rendered scene with the UI on "
                    "top. Pass a hub 'target' (client:0, a pid) or a raw 'pid'. The longer side is "
                    "downscaled to max_size. Works windowed or borderless; exclusive fullscreen "
                    "and a minimised window cannot be captured."
                ),
            )
            def screenshot(
                target: str | None = None, pid: int | None = None, max_size: int = 1568
            ) -> Image | str:
                def run() -> Image:
                    resolved = pid if pid is not None else self._hub.find(target).hello.state.pid
                    return Image(data=capture_window.capture(resolved, max_size), format="png")

                return self._guard(run)

        if IS_WINDOWS and self._setup_type == NATIVE_SETUP:
            self._register_vs()

    def _register_vs(self) -> None:
        from .vs import VisualStudio

        server = self._server
        vs = VisualStudio()

        def call(fn: Any, *args: Any) -> str:
            return self._guard(lambda: json.dumps(vs.run(fn, *args)))

        @server.tool(
            name="vs_info",
            description=(
                "List the running Visual Studio 2022 instances with their solution, debugger mode "
                "and active configuration. With several open, pass 'solution' (part of a solution "
                "path) to choose which one the other vs_ tools drive; an empty string clears it."
            ),
        )
        def vs_info(solution: str | None = None) -> str:
            return call(vs.info, solution)

        @server.tool(
            name="vs_init",
            description=(
                "Prepare the vs_ tools for the open Visual Studio: resolves the instance and reads "
                "the project index, which otherwise lands on the first vs_build (a COM round trip "
                "per project and solution folder). Calling it again re-reads the index, so it is "
                "also the way to pick up a project added to the solution mid-session. The other vs_ "
                "tools work without it - they just pay that cost on their first call."
            ),
        )
        def vs_init() -> str:
            return call(vs.init)

        @server.tool(
            name="vs_build",
            description=(
                "Build in the user's open Visual Studio, exactly as pressing Build there would, "
                "with the active configuration. Call vs_init once first to keep the project index "
                "off this call. Pass 'project' (e.g. 'server_scripting') to build "
                "one project and its dependencies, or omit it for the whole solution. Returns "
                "'in_progress' if the build is still running, so poll vs_build_status: the wait is "
                "clamped to stay inside the transport's request timeout, and the build is started "
                "asynchronously either way. Refused while debugging. Running processes lock their "
                "binaries, so stop the client first or the link fails with LNK1168."
            ),
        )
        def vs_build(project: str | None = None, wait_s: float = 5) -> str:
            return call(vs.build, project, wait_s)

        @server.tool(
            name="vs_build_status",
            description="Report on the last or running Visual Studio build: errors, warnings, output tail.",
        )
        def vs_build_status() -> str:
            return call(vs.build_status)

        @server.tool(
            name="vs_attach",
            description=(
                "Attach the user's Visual Studio debugger to a process. Pass a hub 'target' "
                "(server, client:0, a pid) or a raw 'pid' for a process the hub does not know."
            ),
        )
        def vs_attach(target: str | None = None, pid: int | None = None) -> str:
            def run() -> str:
                resolved = pid if pid is not None else self._hub.find(target).hello.state.pid
                return json.dumps(vs.run(vs.attach, resolved))

            return self._guard(run)

        @server.tool(
            name="vs_detach",
            description="Detach the Visual Studio debugger from everything, leaving the processes running.",
        )
        def vs_detach() -> str:
            return call(vs.detach)

        @server.tool(
            name="vs_breakpoints",
            description="List the breakpoints set in Visual Studio.",
        )
        def vs_breakpoints() -> str:
            return call(vs.breakpoints)

        @server.tool(
            name="vs_breakpoint_add",
            description=(
                "Set a breakpoint at a source file and line, optionally with a C++ condition that "
                "must be true for it to stop. Works before or after attaching."
            ),
        )
        def vs_breakpoint_add(file: str, line: int, condition: str | None = None) -> str:
            return call(vs.add_breakpoint, file, line, condition)

        @server.tool(
            name="vs_breakpoint_remove",
            description="Remove the breakpoints at a source file and line.",
        )
        def vs_breakpoint_remove(file: str, line: int) -> str:
            return call(vs.remove_breakpoint, file, line)

        @server.tool(
            name="vs_control",
            description=(
                "Drive the attached debugger: 'break', 'continue', 'step_over', 'step_into' or "
                "'step_out'. Waits up to wait_s for it to stop again and returns the state: break "
                "reason, stack and locals expanded 'depth' levels. A 'continue' that hits nothing "
                "within wait_s returns while still running; call vs_state later."
            ),
        )
        def vs_control(action: str, wait_s: float = 10, depth: int = 1) -> str:
            return call(vs.control, action, wait_s, depth)

        @server.tool(
            name="vs_state",
            description=(
                "The debugger's current state. When stopped: why, the current thread's stack with "
                "file and line, and the selected frame's locals expanded 'depth' levels."
            ),
        )
        def vs_state(depth: int = 1) -> str:
            return call(vs.state, depth)

        @server.tool(
            name="vs_select_frame",
            description="Make a stack frame (index from vs_state) current, so its locals and expressions are in scope.",
        )
        def vs_select_frame(index: int, depth: int = 1) -> str:
            return call(vs.select_frame, index, depth)

        @server.tool(
            name="vs_eval",
            description=(
                "Evaluate a C++ expression in the current frame while stopped, the way the Watch "
                "window does, expanding members 'depth' levels. Side effects are allowed, as in the "
                "Immediate window, so prefer reads."
            ),
        )
        def vs_eval(expression: str, depth: int = 1) -> str:
            return call(vs.evaluate, expression, depth)

    def _wait_for(self, target: str, wanted: set[str], timeout_s: float) -> str:
        # The transport caps a request at 30s, so the wait must answer inside it; call again to wait longer.
        timeout_s = max(0.0, min(timeout_s, WAIT_FOR_MAX_TIMEOUT_S))

        started = time.monotonic()
        seen: AgentConnection | None = None

        while True:
            try:
                agent: AgentConnection | None = self._hub.find(target)
            except AgentError:
                agent = None

            if seen is not None and (agent is None or agent.closed):
                return _error(
                    f"{target} disconnected from the hub while waiting for {sorted(wanted)}; "
                    f"read_log(pid={seen.hello.state.pid}) for why"
                )

            if agent is not None:
                seen = agent

                try:
                    agent.ping(timeout=WAIT_PING_TIMEOUT_S)
                except AgentError:
                    pass

                current = agent.status.phase
                if CONNECTED in wanted or current in wanted:
                    payload = _describe(agent, self._hub.liveness(agent))
                    payload["waited_s"] = round(time.monotonic() - started, 1)
                    return json.dumps(payload)

                if _is_terminal_client(agent):
                    return _error(_replace_guidance(agent, "wait_for"))

            elif target.isdigit():
                code = self._supervisor.exit_code(int(target))
                if code is not None:
                    return _error(
                        f"pid {target} exited with code {code} before connecting; "
                        f"read_log(pid={target}) for why"
                    )

            if time.monotonic() - started >= timeout_s:
                current = agent.status.phase if agent is not None else "not connected"
                return _error(
                    f"timed out after {timeout_s:.0f}s waiting for {sorted(wanted)}; "
                    f"{target} is {current}. The wait is capped at {WAIT_FOR_MAX_TIMEOUT_S:.0f}s to "
                    "stay inside the transport's request timeout, so call again to keep waiting."
                )

            time.sleep(WAIT_POLL_INTERVAL_S)

    def _guard(self, fn: Any) -> str:
        try:
            return fn()
        except AgentError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(f"{type(exc).__name__}: {exc}")

    def _profile(self, name: str) -> Profile:
        profile = self._profiles.get(name)
        if profile is None:
            raise AgentError(f"no profile '{name}'; known: {sorted(self._profiles)}")
        return profile

    def _request_shutdown(self, pid: int) -> bool:
        agent = self._hub.agent_for_pid(pid)

        if agent is None:
            return False

        try:
            agent.call("shutdown", None, timeout=SHUTDOWN_REQUEST_TIMEOUT_S)
        except AgentError:
            return False

        return True

    def _logfile_for(self, target: str | None, pid: int | None) -> Path | None:
        if pid is not None:
            target = str(pid)

        try:
            logfile = self._hub.find(target).hello.state.logfile
            if logfile:
                return Path(logfile)
        except AgentError:
            pass

        if target is None or not target.isdigit():
            return None

        for entry in self._hub.tombstones():
            if str(entry["pid"]) == target and entry.get("logfile"):
                return Path(entry["logfile"])

        return self._supervisor.logfile_for(int(target))

    def _bound_address(self, server_profile: str) -> str | None:
        for agent in self._hub.servers():
            if self._hub.liveness(agent) == DEAD:
                continue
            if self._supervisor.profile_for_pid(agent.hello.state.pid) != server_profile:
                continue

            try:
                result = self._exec(agent, "sv_bindaddr", 1200)
            except (AgentError, OSError):
                continue

            for line in result.lines:
                if line.text.startswith("Value: "):
                    return _normalise_bindaddr(line.text[len("Value: ") :].strip())

        return None

    def _resolve_address(self, server_profile: str | None) -> str | None:
        if server_profile is None:
            return None

        live = self._bound_address(server_profile)
        if live is not None:
            return live

        profile = self._profiles.get(server_profile)
        if profile is None or not profile.bindaddr:
            return None

        return _normalise_bindaddr(profile.bindaddr)

    def _exec(self, agent: AgentConnection, command: str, wait_ms: int) -> StreamResult:
        return StreamResult.from_wire(agent.call("console.exec", {"command": command, "wait_ms": wait_ms}))

    def _bundle(
        self,
        agent: AgentConnection,
        name: str,
        verb: str,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        clients = [a for a in self._hub.agents() if a.hello.state.realm == CLIENT]
        joining = [a for a in clients if a.status.phase in JOINING_CLIENT_PHASES]

        if joining:
            named = ", ".join(f"client:{a.hello.state.slot} is {a.status.phase}" for a in joining)
            return {
                "bundle": name,
                "verb": verb,
                "confirmed": False,
                "error": (
                    f"bundle_{verb} is deferred while a client is joining the session: {named}. "
                    "Wait for it to settle, then retry; list_processes shows each phase."
                ),
            }

        expendable = [a for a in clients if _is_terminal_client(a)]
        payload: dict[str, Any] = {"bundle": name, "verb": verb}

        if expendable:
            payload["terminal_clients"] = [
                {
                    "target": f"client:{a.hello.state.slot}",
                    "phase": a.status.phase,
                    "pid": a.hello.state.pid,
                }
                for a in expendable
            ]
            payload["guidance"] = " ".join(
                _replace_guidance(a, f"bundle_{verb}") for a in expendable
            )

        cursor = agent.drain_stream(0).cursor
        self._exec(agent, f"{verb} {name}", 250)

        markers = {
            "start": (f"Started bundle {name}",),
            "stop": (f"Stopped bundle {name}", f"Bundle {name} was already stopped"),
            "restart": (f"Restarted bundle {name}", f"Started bundle {name}"),
        }[verb]

        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            result = agent.drain_stream(cursor)
            cursor = result.cursor

            for line in result.lines:
                collected.append({"source": line.source, "module": line.module, "text": line.text})
                if any(marker in line.text for marker in markers):
                    return {**payload, "confirmed": True, "cursor": cursor, "lines": collected}

            time.sleep(BUNDLE_POLL_INTERVAL_S)

        return {
            **payload,
            "confirmed": False,
            "cursor": cursor,
            "lines": collected,
            "note": (
                f"the server did not report {verb} within {timeout:.0f}s. The command is queued "
                "and applied on the bundle monitor's next tick, so this may mean it was rejected "
                "or that the tick has not run; check lines before retrying."
            ),
        }
