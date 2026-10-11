from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import IS_WINDOWS

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010

GRACEFUL_STOP_TIMEOUT_S = 10.0
FORCED_STOP_TIMEOUT_S = 10.0

SERVER = "server"
CLIENT = "client"
KINDS = (SERVER, CLIENT)

UGC_SETUP = "ugc"
NATIVE_SETUP = "native"
SETUP_TYPES = (UGC_SETUP, NATIVE_SETUP)


@dataclass
class Profile:
    name: str
    kind: str
    install_dir: Path
    bindaddr: str | None = None
    voip_addr: str | None = None
    launcher: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)

    def command(self, arguments: list[str]) -> list[str]:
        return [*self.launcher, str(self.binary()), *arguments]

    def binary(self) -> Path:
        if self.kind == CLIENT:
            return self.install_dir / "BlackICE.exe"

        windows_binary = IS_WINDOWS or bool(self.launcher)

        return self.install_dir / ("REServer.exe" if windows_binary else "REServer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "install_dir": str(self.install_dir),
            "bindaddr": self.bindaddr,
            "voip_addr": self.voip_addr,
            "launcher": self.launcher,
            "extra_args": self.extra_args,
        }

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "install_dir": str(self.install_dir),
        }
        if self.bindaddr:
            payload["bindaddr"] = self.bindaddr
        if self.voip_addr:
            payload["voip_addr"] = self.voip_addr
        if self.launcher:
            payload["launcher"] = self.launcher
        if self.extra_args:
            payload["extra_args"] = self.extra_args
        return payload


@dataclass
class Spawned:
    profile: str
    kind: str
    pid: int
    process: subprocess.Popen
    started_at: float
    workdir: Path

    def to_dict(self, connected: bool, target: str | None = None) -> dict[str, Any]:
        code = self.process.poll()
        payload = {
            "profile": self.profile,
            "kind": self.kind,
            "pid": self.pid,
            "target": target or str(self.pid),
            "running": code is None,
            "connected": connected,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
        }

        if code is not None:
            payload["exit_code"] = code

            payload["signal"] = -code if code < 0 else None
            if hasattr(signal, "SIGTRAP") and code == -signal.SIGTRAP:
                payload["note"] = "terminated by SIGTRAP, which is how a VERIFY failure dies"

        return payload


def parse_profile(name: str, entry: dict[str, Any]) -> Profile:
    kind = entry.get("kind")

    if kind is None:
        raise ValueError(f"profile '{name}' has no kind, expected one of {list(KINDS)}")

    if kind not in KINDS:
        raise ValueError(f"profile '{name}' has kind '{kind}', expected one of {list(KINDS)}")

    if "install_dir" not in entry:
        raise ValueError(f"profile '{name}' has no install_dir")

    return Profile(
        name=name,
        kind=kind,
        install_dir=Path(entry["install_dir"]).expanduser(),
        bindaddr=entry.get("bindaddr"),
        voip_addr=entry.get("voip_addr"),
        launcher=[str(part) for part in entry.get("launcher", [])],
        extra_args=list(entry.get("extra_args", [])),
    )


def load_setup_type(config_path: Path) -> str:
    if not config_path.is_file():
        return UGC_SETUP

    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return UGC_SETUP

    value = data.get("setup_type") if isinstance(data, dict) else None

    return value if value in SETUP_TYPES else UGC_SETUP


def load_profiles(config_path: Path) -> dict[str, Profile]:
    if not config_path.is_file():
        return {}

    data = json.loads(config_path.read_text())
    profiles: dict[str, Profile] = {}

    for name, entry in data.get("profiles", {}).items():
        profiles[name] = parse_profile(name, entry)

    return profiles


def save_profile(config_path: Path, profile: Profile, overwrite: bool = False) -> None:
    if config_path.is_file():
        data = json.loads(config_path.read_text())
    else:
        data = {"profiles": {}}

    profiles = data.setdefault("profiles", {})

    if profile.name in profiles and not overwrite:
        raise ValueError(
            f"profile '{profile.name}' already exists at {config_path}. "
            "Pass overwrite=true to replace it."
        )

    profiles[profile.name] = profile.to_wire()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n")


class Supervisor:
    def __init__(self, hub_endpoint: tuple[str, int], token: str, child_console: bool | None = None) -> None:
        self._endpoint = hub_endpoint
        self._token = token
        self._child_console = IS_WINDOWS if child_console is None else child_console
        self._spawned: dict[int, Spawned] = {}
        self.request_shutdown: Callable[[int], bool] | None = None

    def mcp_args(self) -> list[str]:
        host, port = self._endpoint
        arguments = ["--mcp", "--mcp-addr", f"{host}:{port}"]

        if self._token:
            arguments += ["--mcp-token", self._token]

        return arguments

    def spawn_server(self, profile: Profile) -> Spawned:
        arguments = []
        if profile.bindaddr:
            arguments += ["--bindaddr", profile.bindaddr]
        if profile.voip_addr:
            arguments += ["--voip-addr", profile.voip_addr]

        return self._launch(profile, SERVER, arguments)

    def spawn_client(self, profile: Profile) -> Spawned:
        return self._launch(profile, CLIENT, [])

    def _launch(self, profile: Profile, kind: str, arguments: list[str]) -> Spawned:
        if profile.kind != kind:
            raise ValueError(f"profile '{profile.name}' is a {profile.kind} profile")

        binary = profile.binary()
        if not binary.is_file():
            raise FileNotFoundError(f"no {binary.name} in '{profile.install_dir}'")

        command = profile.command([*self.mcp_args(), *profile.extra_args, *arguments])
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

        if not IS_WINDOWS:
            options["start_new_session"] = True
        elif self._child_console and kind == SERVER:
            # Without inherited handles it gets its own console instead of the hub's MCP stdio
            options = {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NEW_CONSOLE}
        else:
            options["creationflags"] = CREATE_NEW_PROCESS_GROUP

        process = subprocess.Popen(command, cwd=str(profile.install_dir), **options)

        spawned = Spawned(
            profile=profile.name,
            kind=kind,
            pid=process.pid,
            process=process,
            started_at=time.monotonic(),
            workdir=profile.install_dir,
        )
        self._spawned[process.pid] = spawned
        return spawned

    def list_spawned(self, targets: dict[int, str]) -> list[dict[str, Any]]:
        return [
            entry.to_dict(entry.pid in targets, targets.get(entry.pid))
            for entry in self._spawned.values()
        ]

    def exit_code(self, pid: int) -> int | None:
        entry = self._spawned.get(pid)
        return entry.process.poll() if entry is not None else None

    def profile_for_pid(self, pid: int) -> str | None:
        entry = self._spawned.get(pid)
        return entry.profile if entry is not None else None

    def logfile_for(self, pid: int) -> Path | None:
        entry = self._spawned.get(pid)
        if entry is None:
            return None

        return max((entry.workdir / "logs").glob("*.txt"), key=lambda p: p.stat().st_mtime, default=None)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if IS_WINDOWS:
            process.terminate()
            return

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            process.terminate()

    def stop(self, pid: int) -> dict[str, Any]:
        entry = self._spawned.get(pid)
        if entry is None:
            raise PermissionError(
                f"pid {pid} was not started by this hub; refusing to stop it. "
                "Attached processes can be read and driven, but not killed."
            )

        if entry.process.poll() is not None:
            return {"pid": pid, "stopped": False, "note": "already exited"}

        if self._ask_to_shutdown(pid) and self._wait_for_exit(entry.process, GRACEFUL_STOP_TIMEOUT_S):
            del self._spawned[pid]
            return {"pid": pid, "stopped": True, "graceful": True}

        self._terminate(entry.process)

        forced = not self._wait_for_exit(entry.process, FORCED_STOP_TIMEOUT_S)
        if forced:
            entry.process.kill()
            self._wait_for_exit(entry.process, FORCED_STOP_TIMEOUT_S)

        del self._spawned[pid]
        return {"pid": pid, "stopped": True, "graceful": False, "forced": forced}

    def _ask_to_shutdown(self, pid: int) -> bool:
        if self.request_shutdown is None:
            return False

        try:
            return self.request_shutdown(pid)
        except Exception:
            return False

    @staticmethod
    def _wait_for_exit(process: subprocess.Popen, timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def shutdown(self) -> None:
        for pid in list(self._spawned):
            self.stop(pid)
