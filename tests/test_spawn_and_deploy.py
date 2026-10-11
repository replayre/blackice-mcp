import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from blackice_mcp.build import stage_bundle
from blackice_mcp.hub import Hub
from blackice_mcp.protocol import AgentError, RealmStatus, StreamResult
from blackice_mcp.server import McpSurface, _connect_refusal, _is_terminal_client
from blackice_mcp.supervisor import CLIENT, SERVER, Profile, Supervisor


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.exits = True

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if not self.exits:
            raise subprocess.TimeoutExpired(self.pid, timeout)

        return 0


class PopenReplacement:
    next_pid = 5000
    calls: list[dict] = []

    def __call__(self, command, **kwargs):
        PopenReplacement.next_pid += 1
        PopenReplacement.calls.append({"command": list(command), **kwargs})
        return FakeProcess(PopenReplacement.next_pid)


class FakeAgent:
    def __init__(self, pid, realm, slot, lines=(), phase="unknown", logfile=None):
        self.closed = False
        self.last_seen = time.monotonic()
        self.ticks_observed = []
        self.status = RealmStatus(phase=phase)
        self.hello = SimpleNamespace(
            state=SimpleNamespace(realm=realm, slot=slot, pid=pid, logfile=logfile)
        )
        self.calls: list[tuple[str, object]] = []
        self._lines = list(lines)

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, params))

        return {
            "cursor": 1,
            "dropped": 0,
            "lines": [
                {"seq": i, "source": "console", "module": "", "text": text}
                for i, text in enumerate(self._lines)
            ],
        }

    def drain_stream(self, cursor):
        return StreamResult.from_wire(self.call("stream.read"))


def _surface(supervisor, profiles, *agents):
    hub = Hub(host="127.0.0.1", port=0)

    for agent in agents:
        state = agent.hello.state
        hub._agents[(state.realm, state.slot)] = agent
        hub._by_pid[state.pid] = agent

    return McpSurface(hub=hub, supervisor=supervisor, profiles=profiles)


def _call_tool(surface, name, arguments):
    result = asyncio.run(surface.server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


@pytest.fixture
def supervisor(monkeypatch):
    PopenReplacement.next_pid = 5000
    PopenReplacement.calls = []
    monkeypatch.setattr("blackice_mcp.supervisor.subprocess.Popen", PopenReplacement())
    return Supervisor(("127.0.0.1", 1), "")


@pytest.fixture
def installed(tmp_path):
    (tmp_path / "REServer").write_text("")
    (tmp_path / "REServer.exe").write_text("")
    (tmp_path / "BlackICE.exe").write_text("")
    return tmp_path


def test_second_client_does_not_evict_the_first(supervisor, installed):
    profile = Profile(name="c", kind=CLIENT, install_dir=installed)

    first = supervisor.spawn_client(profile)
    second = supervisor.spawn_client(profile)

    assert first.pid != second.pid
    assert {entry["pid"] for entry in supervisor.list_spawned({})} == {first.pid, second.pid}


def test_both_clients_can_be_stopped(supervisor, installed):
    profile = Profile(name="c", kind=CLIENT, install_dir=installed)

    first = supervisor.spawn_client(profile)
    second = supervisor.spawn_client(profile)

    assert supervisor.stop(first.pid)["stopped"]
    assert supervisor.stop(second.pid)["stopped"]


def test_a_profile_of_the_wrong_kind_is_refused(supervisor, installed):
    with pytest.raises(ValueError, match="client profile"):
        supervisor.spawn_server(Profile(name="c", kind=CLIENT, install_dir=installed))


def test_stopping_a_connected_process_asks_it_to_shut_down_first(supervisor, installed):
    spawned = supervisor.spawn_client(Profile(name="c", kind=CLIENT, install_dir=installed))

    asked: list[int] = []
    supervisor.request_shutdown = lambda pid: asked.append(pid) or True

    result = supervisor.stop(spawned.pid)

    assert asked == [spawned.pid]
    assert result == {"pid": spawned.pid, "stopped": True, "graceful": True}
    assert spawned.process.terminated is False


def test_a_process_that_ignores_the_shutdown_request_is_terminated_and_killed(
    supervisor, installed
):
    spawned = supervisor.spawn_client(Profile(name="c", kind=CLIENT, install_dir=installed))
    spawned.process.exits = False

    supervisor.request_shutdown = lambda pid: True

    result = supervisor.stop(spawned.pid)

    assert result["graceful"] is False
    assert result["forced"] is True
    assert spawned.process.terminated is True
    assert spawned.process.killed is True


def test_a_process_that_cannot_be_asked_is_terminated_directly(supervisor, installed):
    spawned = supervisor.spawn_client(Profile(name="c", kind=CLIENT, install_dir=installed))

    result = supervisor.stop(spawned.pid)

    assert result == {"pid": spawned.pid, "stopped": True, "graceful": False, "forced": False}
    assert spawned.process.terminated is True


def test_a_process_the_hub_did_not_start_is_not_stopped(supervisor):
    with pytest.raises(PermissionError):
        supervisor.stop(4321)


def test_stop_process_asks_the_client_to_shut_down_before_terminating(supervisor, installed):
    spawned = supervisor.spawn_client(Profile(name="c", kind=CLIENT, install_dir=installed))
    agent = FakeAgent(spawned.pid, "client", 0, phase="session_active")
    surface = _surface(supervisor, {}, agent)

    result = _call_tool(surface, "stop_process", {"pid": spawned.pid})

    assert result["graceful"] is True
    assert spawned.process.terminated is False
    assert ("shutdown", None) in agent.calls


def test_read_log_answers_a_dead_pid_with_that_process_log(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="session_active"))
    surface._hub._tombstones.append(
        {"realm": "client", "slot": 0, "pid": 900, "logfile": "C:/logs/client_0.txt"}
    )

    assert surface._logfile_for(None, 900) == Path("C:/logs/client_0.txt")


def test_read_log_does_not_answer_an_unknown_pid_with_another_process_log(supervisor):
    live = FakeAgent(901, "server", 0, phase="active", logfile="C:/logs/server.txt")
    surface = _surface(supervisor, {}, live)
    surface._hub._tombstones.append(
        {"realm": "client", "slot": 0, "pid": 900, "logfile": "C:/logs/client_0.txt"}
    )

    assert surface._logfile_for(None, 4321) is None


def test_request_shutdown_is_impossible_without_a_connected_process(supervisor):
    assert _surface(supervisor, {})._request_shutdown(4321) is False


def test_request_shutdown_reports_failure_when_the_process_cannot_be_asked(supervisor):
    agent = FakeAgent(4321, "client", 0, phase="session_active")
    surface = _surface(supervisor, {}, agent)

    def unknown_method(method, params=None, timeout=None):
        raise AgentError("shutdown: unknown method")

    agent.call = unknown_method

    assert surface._request_shutdown(4321) is False


def test_spawned_target_is_a_pid_until_the_process_connects(supervisor, installed):
    spawned = supervisor.spawn_server(Profile(name="s", kind=SERVER, install_dir=installed))
    payload = supervisor.list_spawned({})[0]

    assert payload["target"] == str(spawned.pid)
    assert payload["connected"] is False


def test_spawned_target_becomes_realm_slot_once_connected(supervisor, installed):
    spawned = supervisor.spawn_server(Profile(name="s", kind=SERVER, install_dir=installed))
    payload = supervisor.list_spawned({spawned.pid: "server:0"})[0]

    assert payload["target"] == "server:0"
    assert payload["pid"] == spawned.pid
    assert payload["connected"] is True


def test_the_reported_target_is_accepted_by_the_hub(supervisor, installed):
    spawned = supervisor.spawn_server(Profile(name="s", kind=SERVER, install_dir=installed))
    surface = _surface(supervisor, {}, FakeAgent(spawned.pid, "server", 0))

    target = supervisor.list_spawned({spawned.pid: "server:0"})[0]["target"]

    assert surface._hub.find(target) is not None


def test_an_empty_token_is_not_passed_to_the_process(supervisor, installed):
    supervisor.spawn_server(Profile(name="s", kind=SERVER, install_dir=installed))

    assert "--mcp-token" not in PopenReplacement.calls[0]["command"]


def test_a_set_token_is_passed_to_the_process(supervisor, installed):
    Supervisor(("127.0.0.1", 1), "sekrit").spawn_server(
        Profile(name="s", kind=SERVER, install_dir=installed)
    )

    command = PopenReplacement.calls[0]["command"]
    assert command[command.index("--mcp-token") + 1] == "sekrit"


def test_a_server_gets_its_own_console_on_windows(supervisor, installed, monkeypatch):
    monkeypatch.setattr("blackice_mcp.supervisor.IS_WINDOWS", True)

    Supervisor(("127.0.0.1", 1), "", child_console=True).spawn_server(
        Profile(name="s", kind=SERVER, install_dir=installed)
    )

    call = PopenReplacement.calls[0]
    assert call["creationflags"] & 0x00000010
    assert "stdout" not in call


def test_a_server_console_can_be_suppressed(supervisor, installed, monkeypatch):
    monkeypatch.setattr("blackice_mcp.supervisor.IS_WINDOWS", True)

    Supervisor(("127.0.0.1", 1), "", child_console=False).spawn_server(
        Profile(name="s", kind=SERVER, install_dir=installed)
    )

    call = PopenReplacement.calls[0]
    assert not call["creationflags"] & 0x00000010
    assert call["stdout"] is not None


def test_a_client_never_gets_a_console(supervisor, installed, monkeypatch):
    monkeypatch.setattr("blackice_mcp.supervisor.IS_WINDOWS", True)

    Supervisor(("127.0.0.1", 1), "", child_console=True).spawn_client(
        Profile(name="c", kind=CLIENT, install_dir=installed)
    )

    assert not PopenReplacement.calls[0]["creationflags"] & 0x00000010


def test_address_is_learned_from_the_running_server(supervisor, installed):
    profile = Profile(name="dev-server", kind=SERVER, install_dir=installed)
    spawned = supervisor.spawn_server(profile)

    surface = _surface(
        supervisor,
        {"dev-server": profile},
        FakeAgent(spawned.pid, "server", 0, ["ConVar sv_bindaddr", "Value: [::]:20770"]),
    )

    assert surface._resolve_address("dev-server") == "127.0.0.1:20770"


def test_address_falls_back_to_the_profile_when_no_server_is_running(supervisor, installed):
    profile = Profile(name="dev-server", kind=SERVER, install_dir=installed, bindaddr="[::]:20770")

    surface = _surface(supervisor, {"dev-server": profile})

    assert surface._resolve_address("dev-server") == "127.0.0.1:20770"


def test_address_is_not_learned_from_another_profile_server(supervisor, installed):
    mine = Profile(name="mine", kind=SERVER, install_dir=installed)
    other = Profile(name="other", kind=SERVER, install_dir=installed)
    spawned = supervisor.spawn_server(other)

    surface = _surface(
        supervisor,
        {"mine": mine, "other": other},
        FakeAgent(spawned.pid, "server", 0, ["Value: [::]:20770"]),
    )

    assert surface._resolve_address("mine") is None


def test_address_is_none_without_a_profile_or_a_server(supervisor):
    surface = _surface(supervisor, {})

    assert surface._resolve_address("dev-server") is None
    assert surface._resolve_address(None) is None


@pytest.fixture
def ui_bundle(tmp_path):
    source = tmp_path / "bundle"
    (source / "ui" / "_app" / "chunks").mkdir(parents=True)
    (source / "ui" / "fonts").mkdir(parents=True)

    (source / "config.toml").write_text(
        "[General]\n"
        'author = "t"\n'
        'description = "d"\n'
        'version = "1"\n'
        "[Scripting]\n"
        'client_blob = "cl.wasm"\n'
        'server_blob = "sv.wasm"\n'
        "[ReUi]\n"
        'initial_page = "index.html"\n'
        'content_root = "ui/"\n'
        'files = ["ui/index.html", "ui/_app/**", "ui/fonts/**", "ui/sw.js"]\n'
        "[Meta]\n"
        'config_version = "alpha_playtest"\n'
    )
    for name in ("cl.wasm", "sv.wasm"):
        (source / name).write_text("wasm")
    (source / "ui" / "index.html").write_text("<html>")
    (source / "ui" / "sw.js").write_text("sw")
    (source / "ui" / "_app" / "chunks" / "main.js").write_text("js")
    (source / "ui" / "fonts" / "f.woff").write_text("woff")
    return source


def test_deploy_stages_the_web_ui(tmp_path, ui_bundle):
    result = stage_bundle(ui_bundle, tmp_path / "srv", "demo")

    assert result.ok
    assert "ui/index.html" in result.copied
    assert "ui/_app/chunks/main.js" in result.copied
    assert "ui/fonts/f.woff" in result.copied
    assert (result.bundle_dir / "ui" / "_app" / "chunks" / "main.js").is_file()


def test_deploy_reports_manifest_entries_that_did_not_land(tmp_path, ui_bundle):
    (ui_bundle / "ui" / "sw.js").unlink()

    result = stage_bundle(ui_bundle, tmp_path / "srv", "demo")

    assert result.ok is False
    assert "ui/sw.js" in result.missing


def test_deploy_treats_glob_entries_as_satisfied_by_any_match(tmp_path, ui_bundle):
    result = stage_bundle(ui_bundle, tmp_path / "srv", "demo")

    assert result.missing == []


def test_connect_is_refused_while_the_client_is_still_initializing():
    refusal = _connect_refusal(FakeAgent(900, "client", 0, phase="initializing"))

    assert refusal is not None
    assert "initializing" in refusal
    assert "menu" in refusal


def test_connect_is_allowed_once_the_client_is_at_the_menu():
    assert _connect_refusal(FakeAgent(900, "client", 0, phase="menu")) is None


def test_connect_to_a_disconnected_client_comes_with_a_replacement_recipe():
    refusal = _connect_refusal(FakeAgent(9421, "client", 0, phase="disconnected"))

    assert "disconnected" in refusal
    assert "pid=9421" in refusal
    assert "stop_process" in refusal
    assert "spawn_client" in refusal
    assert "connect_client" in refusal


def test_connect_to_a_client_already_in_a_session_follows_the_same_rule():
    refusal = _connect_refusal(FakeAgent(900, "client", 0, phase="session_active"))

    assert "stop_process" in refusal


def test_a_failed_client_is_terminal_too():
    refusal = _connect_refusal(FakeAgent(900, "client", 0, phase="failed"))

    assert "stop_process" in refusal


def test_a_server_is_never_terminal():
    assert _is_terminal_client(FakeAgent(900, "server", 0, phase="disconnected")) is False


def test_bundle_restart_is_deferred_while_a_client_is_joining(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="connecting"))

    result = surface._bundle(FakeAgent(901, "server", 0), "gamemodes", "restart")

    assert result["confirmed"] is False
    assert "joining" in result["error"]
    assert "client:0" in result["error"]
    assert "connecting" in result["error"]


def test_bundle_restart_is_allowed_when_every_client_is_session_active(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="session_active"))

    result = surface._bundle(FakeAgent(901, "server", 0, ["Started bundle x"]), "x", "restart")

    assert "error" not in result


def test_a_menu_client_does_not_block_a_bundle_verb(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="menu"))

    result = surface._bundle(FakeAgent(901, "server", 0, ["Started bundle x"]), "x", "restart")

    assert result["confirmed"] is True
    assert "error" not in result


def test_a_client_that_can_only_be_stopped_does_not_block_a_bundle_verb(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="disconnected"))

    result = surface._bundle(FakeAgent(901, "server", 0, ["Started bundle x"]), "x", "restart")

    assert result["confirmed"] is True
    assert result["terminal_clients"] == [
        {"target": "client:0", "phase": "disconnected", "pid": 900}
    ]
    assert "stop_process" in result["guidance"]


def test_a_bundle_verb_reports_terminal_clients_even_when_it_is_not_confirmed(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="disconnected"))

    result = surface._bundle(FakeAgent(901, "server", 0), "x", "restart", timeout=0.0)

    assert result["confirmed"] is False
    assert result["terminal_clients"][0]["target"] == "client:0"


def test_console_exec_refuses_a_client_that_can_only_be_stopped(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="disconnected"))

    payload = _call_tool(surface, "console_exec", {"command": "cyberchat-open", "target": "client:0"})

    assert "stop_process" in payload["error"]
    assert "pid=900" in payload["error"]


def test_console_exec_still_drives_a_client_in_a_session(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "client", 0, phase="session_active"))

    payload = _call_tool(surface, "console_exec", {"command": "cyberchat-open", "target": "client:0"})

    assert "error" not in payload


def test_bundle_commands_are_not_gated_on_the_servers_own_phase(supervisor):
    surface = _surface(supervisor, {}, FakeAgent(900, "server", 0, phase="starting"))

    result = surface._bundle(FakeAgent(901, "server", 0, ["Started bundle x"]), "x", "start")

    assert "error" not in result


def test_bundle_stop_is_confirmed_only_by_the_servers_own_line(supervisor):
    surface = _surface(supervisor, {})

    result = surface._bundle(FakeAgent(901, "server", 0), "gamemodes", "stop", timeout=0.05)

    assert result["confirmed"] is False
    assert "did not report stop" in result["note"]


def test_bundle_stop_is_confirmed_when_the_server_reports_it(supervisor):
    surface = _surface(supervisor, {})

    result = surface._bundle(
        FakeAgent(901, "server", 0, ["Stopped bundle gamemodes"]), "gamemodes", "stop", timeout=0.05
    )

    assert result["confirmed"] is True


def test_bundle_stop_accepts_already_stopped_as_evidence(supervisor):
    surface = _surface(supervisor, {})

    result = surface._bundle(
        FakeAgent(901, "server", 0, ["Bundle gamemodes was already stopped"]),
        "gamemodes",
        "stop",
        timeout=0.05,
    )

    assert result["confirmed"] is True


def test_a_bundle_verb_is_not_confirmed_by_another_verbs_line(supervisor):
    surface = _surface(supervisor, {})

    result = surface._bundle(
        FakeAgent(901, "server", 0, ["Started bundle gamemodes"]), "gamemodes", "stop", timeout=0.05
    )

    assert result["confirmed"] is False
