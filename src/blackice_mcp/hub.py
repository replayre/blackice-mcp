from __future__ import annotations

import socket
import threading
import time
from typing import Any

from .agent import AgentConnection
from .protocol import DEFAULT_PORT, AgentError, Hello

HEARTBEAT_INTERVAL_S = 10.0
FROZEN_AFTER_S = 30.0

ALIVE = "alive"
STALLED = "stalled"
FROZEN = "frozen"
DEAD = "dead"


class Hub:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, token: str = "") -> None:
        self._host = host
        self._port = port
        self._token = token
        self._lock = threading.Lock()
        self._agents: dict[tuple[str, int], AgentConnection] = {}
        self._by_pid: dict[int, AgentConnection] = {}
        self._tombstones: list[dict[str, Any]] = []
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._beat_thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self._host, self._port)

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._host, self._port))
        listener.listen(16)
        listener.settimeout(0.25)

        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self._beat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._beat_thread.start()

    def stop(self) -> None:
        self._stop.set()

        if self._beat_thread is not None:
            self._beat_thread.join(timeout=2.0)

        if self._thread is not None:
            self._thread.join(timeout=2.0)

        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

        with self._lock:
            agents = list(self._agents.values())
            self._agents.clear()
            self._by_pid.clear()

        for agent in agents:
            agent.close()

    def _accept_loop(self) -> None:
        assert self._listener is not None

        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                self._reap_closed()
                continue
            except OSError:
                break

            threading.Thread(target=self._register, args=(conn,), daemon=True).start()

    def _register(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        buffer = ""

        try:
            while "\n" not in buffer:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                buffer += chunk.decode(errors="replace")
        except OSError:
            conn.close()
            return

        line, _ = buffer.split("\n", 1)
        hello = Hello.parse(line)

        if hello is None:
            conn.close()
            return

        if self._token and hello.token != self._token:
            conn.close()
            return

        agent = AgentConnection(conn, hello)

        with self._lock:
            previous = self._agents.get(agent.key)
            if previous is not None:
                self._by_pid.pop(previous.hello.state.pid, None)
                previous.close()

            self._agents[agent.key] = agent
            self._by_pid[hello.state.pid] = agent

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            with self._lock:
                agents = list(self._agents.values())

            for agent in agents:
                if agent.closed:
                    continue

                try:
                    agent.ping(timeout=FROZEN_AFTER_S / 3)
                    with self._lock:
                        agent.ticks_observed.append(agent.last_tick)
                except AgentError:
                    pass

    def liveness(self, agent: AgentConnection) -> str:
        if agent.closed:
            return DEAD

        if time.monotonic() - agent.last_seen > FROZEN_AFTER_S:
            return FROZEN

        ticks = agent.ticks_observed
        if len(ticks) == 3 and len(set(ticks)) == 1:
            return STALLED

        return ALIVE

    def _reap_closed(self) -> None:
        with self._lock:
            agents = list(self._agents.values())

        for agent in agents:
            if not agent.closed:
                continue

            with self._lock:
                self._agents.pop(agent.key, None)
                self._by_pid.pop(agent.hello.state.pid, None)
                self._tombstones.append(
                    {
                        "realm": agent.hello.state.realm,
                        "slot": agent.hello.state.slot,
                        "pid": agent.hello.state.pid,
                        "build_commit": agent.hello.state.build_commit,
                        "workdir": agent.hello.state.workdir,
                        "logfile": agent.hello.state.logfile,
                        "died_at": time.time(),
                    }
                )

    def tombstones(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._tombstones)

    def servers(self) -> list[AgentConnection]:
        self._reap_closed()
        with self._lock:
            return [a for a in self._agents.values() if a.hello.state.realm == "server"]

    def agent_for_pid(self, pid: int) -> AgentConnection | None:
        self._reap_closed()

        with self._lock:
            return self._by_pid.get(pid)

    def agents(self) -> list[AgentConnection]:
        self._reap_closed()
        with self._lock:
            return list(self._agents.values())

    def find(self, target: str | None) -> AgentConnection:
        self._reap_closed()

        with self._lock:
            agents = list(self._agents.values())
            by_pid = dict(self._by_pid)

        if not agents:
            raise AgentError("no processes are connected")

        if not target:
            if len(agents) == 1:
                return agents[0]
            raise AgentError(f"{len(agents)} processes are connected; specify a target")

        if target.isdigit():
            agent = by_pid.get(int(target))
            if agent is None:
                raise AgentError(f"no connected process with pid {target}")
            return agent

        if ":" in target:
            realm, _, slot = target.partition(":")
            if not slot.isdigit():
                raise AgentError(f"invalid target '{target}', expected realm:slot")
            for agent in agents:
                if agent.hello.state.realm == realm and agent.hello.state.slot == int(slot):
                    return agent
            raise AgentError(f"no connected process matching '{target}'")

        matches = [agent for agent in agents if agent.hello.state.realm == target]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise AgentError(f"no connected process in realm '{target}'")
        raise AgentError(f"{len(matches)} processes in realm '{target}'; specify realm:slot or pid")

    def check_server_spawn_allowed(self) -> None:
        # A frozen server still holds the port, so only a dead one can be replaced
        live = [a for a in self.servers() if self.liveness(a) != DEAD]

        if not live:
            return

        states = ", ".join(f"{self.liveness(a)} pid {a.hello.state.pid}" for a in live)
        raise AgentError(
            f"a server is already running ({states}). Stop it before starting another; "
            "two servers collide on their bind address."
        )
