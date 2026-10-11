from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from typing import Any

from .protocol import AgentError, Hello, RealmStatus, StreamResult, make_request

READ_TIMEOUT = 0.25
CALL_TIMEOUT = 30.0


class AgentConnection:
    def __init__(self, conn: socket.socket, hello: Hello) -> None:
        self._conn = conn
        self._hello = hello
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._responses: dict[int, dict[str, Any]] = {}
        self._buffer = ""
        self._closed = False
        self._last_seen = time.monotonic()
        self._live_status: RealmStatus | None = None
        self.last_tick = 0
        self.ticks_observed: deque[int] = deque(maxlen=3)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @property
    def hello(self) -> Hello:
        return self._hello

    @property
    def key(self) -> tuple[str, int]:
        return (self._hello.state.realm, self._hello.state.slot)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_seen(self) -> float:
        return self._last_seen

    @property
    def status(self) -> RealmStatus:
        return self._live_status or self._hello.state.status

    def close(self) -> None:
        self._closed = True
        try:
            self._conn.close()
        except OSError:
            pass

    def _read_loop(self) -> None:
        while not self._closed:
            self._conn.settimeout(READ_TIMEOUT)
            try:
                chunk = self._conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                self._closed = True
                return

            if not chunk:
                self._closed = True
                return

            self._last_seen = time.monotonic()
            self._buffer += chunk.decode(errors="replace")

            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        if not isinstance(data, dict) or data.get("id") is None:
            return

        with self._lock:
            self._responses[int(data["id"])] = data

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT) -> Any:
        if self._closed:
            raise AgentError("agent connection is closed")

        with self._lock:
            request_id = self._next_id
            self._next_id += 1

        try:
            with self._write_lock:
                self._conn.sendall(make_request(request_id, method, params).encode() + b"\n")
        except OSError as exc:
            self._closed = True
            raise AgentError(f"failed to send {method}: {exc}") from exc

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            with self._lock:
                response = self._responses.pop(request_id, None)
            if response is not None:
                break
            if self._closed:
                raise AgentError(f"agent connection closed while awaiting {method}")
            time.sleep(0.01)
        else:
            raise AgentError(f"{method} timed out after {timeout:.0f}s")

        if "error" in response:
            raise AgentError(f"{method}: {response['error']}")

        return response.get("result")

    def ping(self, timeout: float = 5.0) -> dict[str, Any]:
        result = self.call("ping", None, timeout=timeout)
        self.last_tick = int(result.get("tick", 0))

        if result.get("status") is not None:
            self._live_status = RealmStatus.from_wire(result["status"])

        return result

    def drain_stream(self, cursor: int = 0) -> StreamResult:
        return StreamResult.from_wire(self.call("stream.read", {"cursor": cursor}))
