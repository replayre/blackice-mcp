import json
from types import SimpleNamespace

import pytest

from blackice_mcp.protocol import AgentError, RealmStatus
from blackice_mcp.server import McpSurface


class FakeAgent:
    def __init__(self, phases, realm="client", pid=100):
        self._phases = list(phases)
        self.status = RealmStatus(phase=self._phases[0])
        self.hello = SimpleNamespace(
            state=SimpleNamespace(
                realm=realm, slot=0, pid=pid, workdir="", build_commit="", runtime_stage=0, logfile=""
            )
        )
        self.closed = False

    def ping(self, timeout):
        if len(self._phases) > 1:
            self._phases.pop(0)
        self.status = RealmStatus(phase=self._phases[0])


class FakeHub:
    def __init__(self, script):
        self._script = list(script)

    def find(self, target):
        agent = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if agent is None:
            raise AgentError("no connected process")
        return agent

    def liveness(self, agent):
        return "alive"


class FakeSupervisor:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code

    def exit_code(self, pid):
        return self._exit_code


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("blackice_mcp.server.WAIT_POLL_INTERVAL_S", 0)


def surface(hub, supervisor=None):
    return McpSurface(hub, supervisor or FakeSupervisor(), api_docs=SimpleNamespace(base_url=""))


def test_returns_once_the_phase_is_reached():
    agent = FakeAgent(["initializing", "initializing", "menu"])
    result = json.loads(surface(FakeHub([None, agent]))._wait_for("100", {"menu"}, 5))

    assert result["phase"] == "menu"


def test_connected_returns_as_soon_as_it_reaches_the_hub():
    agent = FakeAgent(["initializing"])
    result = json.loads(surface(FakeHub([None, None, agent]))._wait_for("100", {"connected"}, 5))

    assert result["phase"] == "initializing"


def test_a_pid_that_exits_before_connecting_fails_fast():
    result = json.loads(surface(FakeHub([None]), FakeSupervisor(exit_code=3))._wait_for("100", {"menu"}, 60))

    assert "exited with code 3" in result["error"]


def test_a_dropped_connection_fails_fast():
    agent = FakeAgent(["connecting"])
    result = json.loads(surface(FakeHub([agent, None]))._wait_for("client:0", {"session_active"}, 60))

    assert "disconnected from the hub" in result["error"]


def test_a_disconnected_client_fails_fast():
    agent = FakeAgent(["connecting", "disconnected"])
    result = json.loads(surface(FakeHub([agent]))._wait_for("client:0", {"session_active"}, 60))

    assert "disconnected" in result["error"]


def test_times_out_with_the_current_phase():
    agent = FakeAgent(["connecting"])
    result = json.loads(surface(FakeHub([agent]))._wait_for("client:0", {"session_active"}, 0))

    assert "timed out" in result["error"] and "connecting" in result["error"]
