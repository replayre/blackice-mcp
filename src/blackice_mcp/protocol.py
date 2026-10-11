from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PORT = 8137


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class RealmStatus:
    phase: str = "unknown"
    detail: str = ""

    @classmethod
    def from_wire(cls, data: dict[str, Any] | None) -> RealmStatus:
        if not isinstance(data, dict):
            return cls()

        return cls(phase=str(data.get("phase", "unknown")), detail=str(data.get("detail", "")))


@dataclass(frozen=True)
class ProcessState:
    realm: str
    slot: int
    pid: int
    workdir: str
    build_commit: str
    runtime_stage: int
    logfile: str = ""
    status: RealmStatus = field(default_factory=RealmStatus)

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> ProcessState:
        return cls(
            realm=data["realm"],
            slot=data["slot"],
            pid=data["pid"],
            workdir=data["workdir"],
            build_commit=data["build_commit"],
            runtime_stage=data["runtime_stage"],
            logfile=data.get("logfile", ""),
            status=RealmStatus.from_wire(data.get("status")),
        )


@dataclass(frozen=True)
class StreamLine:
    seq: int
    source: str
    module: str
    text: str


@dataclass
class StreamResult:
    cursor: int
    dropped: int
    lines: list[StreamLine] = field(default_factory=list)

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> StreamResult:
        return cls(
            cursor=data["cursor"],
            dropped=data.get("dropped", 0),
            lines=[
                StreamLine(seq=line["seq"], source=line["source"], module=line["module"], text=line["text"])
                for line in data.get("lines", [])
            ],
        )


@dataclass(frozen=True)
class Hello:
    token: str
    state: ProcessState

    @classmethod
    def parse(cls, line: str) -> Hello | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict) or data.get("method") != "hello":
            return None

        return cls(token=data.get("token", ""), state=ProcessState.from_wire(data["state"]))


def make_request(request_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"id": request_id, "method": method}
    if params is not None:
        payload["params"] = params

    return json.dumps(payload, separators=(",", ":"))
