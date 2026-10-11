from __future__ import annotations

import concurrent.futures
import os
import re
import time
from typing import Any, Callable

import pythoncom
import pywintypes
import win32com.client
from win32com.client.dynamic import CDispatch

DTE_PREFIX = "!VisualStudio.DTE.17.0:"
SOLUTION_FOLDER_KIND = "{66A26720-8FB5-11D2-AA7E-00C04F688DDE}"

# Late-bound DTE has no ToolWindows (and no Error List), so the output window is found by kind
OUTPUT_WINDOW_KIND = "{34E76E81-EE4A-11D0-AE2E-00A0C90FFFC3}"

# RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER
BUSY_HRESULTS = (-2147418111, -2147417846)
BUSY_RETRY_S = 30.0
BUSY_RETRY_INTERVAL_S = 0.2

POLL_INTERVAL_S = 0.25

# The transport caps a request at 30s, so the wait is clamped; the build runs asynchronously anyway.
MAX_BUILD_WAIT_S = 5.0

# dbgDebugMode
DESIGN, BREAK, RUN = 1, 2, 3
MODES = {DESIGN: "design", BREAK: "break", RUN: "run"}

# vsBuildState
BUILD_IN_PROGRESS, BUILD_DONE = 2, 3

# dbgEventReason
BREAK_REASONS = {
    8: "step",
    9: "breakpoint",
    10: "exception_thrown",
    11: "exception_not_handled",
    12: "user_break",
}

# 12>D:\x\a.cpp(42,5): error C2065: 'foo': undeclared identifier
DIAGNOSTIC = re.compile(
    r"^(?:\s*\d+>)?(?P<body>(?P<file>[^:(]*(?::[\\/][^:(]*)?)(?:\((?P<line>\d+)(?:,\d+)*\))?\s*:\s*"
    r"(?:fatal\s+)?(?P<level>error|warning)\s+(?P<code>[A-Z]+\d+)\s*:(?P<message>.*))$"
)

MAX_STACK_FRAMES = 40
MAX_MEMBERS = 50
MAX_BUILD_DIAGNOSTICS = 30
MAX_DIAGNOSTIC_CHARS = 300
BUILD_OUTPUT_TAIL = 60


class VsError(Exception):
    pass


def retry_busy(fn: Callable[[], Any], timeout: float = BUSY_RETRY_S) -> Any:
    deadline = time.monotonic() + timeout

    while True:
        try:
            return fn()
        except pywintypes.com_error as exc:
            if exc.hresult not in BUSY_HRESULTS or time.monotonic() > deadline:
                raise
            time.sleep(BUSY_RETRY_INTERVAL_S)


def _unwrap(value: Any) -> Any:
    return value._obj if isinstance(value, Com) else value


def _wrap(value: Any) -> Any:
    return Com(value) if isinstance(value, CDispatch) else value


class Com:
    __slots__ = ("_obj",)

    def __init__(self, obj: Any) -> None:
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name: str) -> Any:
        def get() -> Any:
            try:
                return getattr(self._obj, name)
            except AttributeError:
                # pywin32 turns a busy rejection into AttributeError; redo the lookup for the real error
                self._obj._oleobj_.GetIDsOfNames(name)
                raise

        value = retry_busy(get)

        # A dispatch object is callable too
        if isinstance(value, CDispatch):
            return Com(value)

        if callable(value):
            def method(*args: Any) -> Any:
                unwrapped = tuple(_unwrap(arg) for arg in args)
                return _wrap(retry_busy(lambda: value(*unwrapped)))

            return method

        return value

    def __setattr__(self, name: str, value: Any) -> None:
        retry_busy(lambda: setattr(self._obj, name, _unwrap(value)))

    def items(self, limit: int | None = None) -> list[Any]:
        count = self.Count if limit is None else min(self.Count, limit)
        return [self.Item(index) for index in range(1, count + 1)]


def _optional(obj: Com, name: str) -> Any:
    try:
        return getattr(obj, name)
    except (AttributeError, pywintypes.com_error):
        return None


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _expression(expr: Com, depth: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": expr.Name, "type": expr.Type, "value": expr.Value}

    if depth > 0:
        members = expr.DataMembers
        if members.Count:
            payload["members"] = [_expression(m, depth - 1) for m in members.items(MAX_MEMBERS)]
            if members.Count > MAX_MEMBERS:
                payload["members_truncated"] = members.Count - MAX_MEMBERS

    return payload


def parse_diagnostics(output: list[str]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    seen: set[str] = set()

    for line in output:
        match = DIAGNOSTIC.match(line)
        if match is None or match["body"] in seen:
            continue

        seen.add(match["body"])
        diagnostics.append(
            {
                "level": match["level"],
                "code": match["code"],
                "message": match["message"].strip()[:MAX_DIAGNOSTIC_CHARS],
                "file": match["file"].strip() or None,
                "line": int(match["line"]) if match["line"] else None,
            }
        )

    diagnostics.sort(key=lambda d: d["level"] != "error")
    return diagnostics


class VisualStudio:
    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="vs-dte",
            initializer=pythoncom.CoInitialize,
        )
        self._solution_filter: str | None = None
        self._project_names: dict[str, str] = {}
        self._project_names_solution: str | None = None

    def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        # DTE objects belong to the STA thread that fetched them
        return self._pool.submit(fn, *args).result()

    def _instances(self) -> list[tuple[int, Com]]:
        rot = pythoncom.GetRunningObjectTable()
        context = pythoncom.CreateBindCtx(0)
        found: list[tuple[int, Com]] = []

        for moniker in rot.EnumRunning():
            name = moniker.GetDisplayName(context, None)
            if not name.startswith(DTE_PREFIX):
                continue

            dispatch = rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
            found.append((int(name[len(DTE_PREFIX) :]), Com(win32com.client.dynamic.Dispatch(dispatch))))

        return found

    def _dte(self) -> Com:
        instances = self._instances()

        if not instances:
            raise VsError("no Visual Studio 2022 instance is running")

        if self._solution_filter:
            matching = [
                dte for _, dte in instances
                if self._solution_filter.lower() in (dte.Solution.FullName or "").lower()
            ]
            if not matching:
                raise VsError(f"no Visual Studio has a solution matching '{self._solution_filter}'")
            return matching[0]

        if len(instances) > 1:
            raise VsError(
                "several Visual Studio instances are running; pick one with "
                "vs_info(solution=<part of its solution path>)"
            )

        return instances[0][1]

    def info(self, solution: str | None) -> dict[str, Any]:
        if solution is not None:
            self._solution_filter = solution or None

        instances = []
        for pid, dte in self._instances():
            build = dte.Solution.SolutionBuild
            instances.append(
                {
                    "pid": pid,
                    "solution": dte.Solution.FullName,
                    "debugger": MODES.get(dte.Debugger.CurrentMode, dte.Debugger.CurrentMode),
                    "configuration": self._configuration(dte) if dte.Solution.FullName else None,
                    "building": build.BuildState == BUILD_IN_PROGRESS,
                }
            )

        return {"instances": instances, "solution_filter": self._solution_filter}

    def _configuration(self, dte: Com) -> str:
        active = dte.Solution.SolutionBuild.ActiveConfiguration
        contexts = active.SolutionContexts

        # The late-bound SolutionConfiguration has no PlatformName, a project context does
        platform = contexts.Item(1).PlatformName if contexts.Count else ""
        return f"{active.Name}|{platform}" if platform else active.Name

    def init(self) -> dict[str, Any]:
        """Resolve the instance and read the project index, so the first vs_build does not pay for it."""
        dte = self._dte()
        solution = dte.Solution.FullName

        self._project_names = self._enumerate_projects(dte)
        self._project_names_solution = solution

        return {
            "solution": solution,
            "configuration": self._configuration(dte),
            "projects": len(set(self._project_names.values())),
        }

    def _project_unique_name(self, dte: Com, name: str) -> str:
        solution = dte.Solution.FullName

        # One walk per solution: a COM round trip per project and folder, and the solution does not change.
        if self._project_names_solution != solution:
            self._project_names = {}
            self._project_names_solution = solution

        wanted = name.lower()
        if wanted not in self._project_names:
            self._project_names.update(self._enumerate_projects(dte))

        unique = self._project_names.get(wanted)
        if unique is None:
            raise VsError(f"no project '{name}' in {solution}")

        return unique

    def _enumerate_projects(self, dte: Com) -> dict[str, str]:
        # Every project in the solution, keyed by both its name and its unique name
        found: dict[str, str] = {}
        pending = dte.Solution.Projects.items()

        while pending:
            project = pending.pop()
            if project is None:
                continue

            if project.Kind == SOLUTION_FOLDER_KIND:
                pending.extend(item.SubProject for item in project.ProjectItems.items())
                continue

            found[project.Name.lower()] = project.UniqueName
            found[project.UniqueName.lower()] = project.UniqueName

        return found

    def build(self, project: str | None, wait_s: float) -> dict[str, Any]:
        dte = self._dte()
        build = dte.Solution.SolutionBuild

        if dte.Debugger.CurrentMode != DESIGN:
            raise VsError("Visual Studio is debugging; vs_detach before building")

        if build.BuildState == BUILD_IN_PROGRESS:
            raise VsError("a build is already running; vs_build_status reports on it")

        if project:
            build.BuildProject(self._configuration(dte), self._project_unique_name(dte, project), False)
        else:
            build.Build(False)

        deadline = time.monotonic() + max(0.0, min(wait_s, MAX_BUILD_WAIT_S))
        while build.BuildState == BUILD_IN_PROGRESS and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)

        return self._build_status(dte)

    def build_status(self) -> dict[str, Any]:
        return self._build_status(self._dte())

    def _build_status(self, dte: Com) -> dict[str, Any]:
        build = dte.Solution.SolutionBuild

        if build.BuildState == BUILD_IN_PROGRESS:
            return {"state": "in_progress", "configuration": self._configuration(dte)}

        output = self._build_output(dte)
        diagnostics = parse_diagnostics(output)
        payload: dict[str, Any] = {
            "state": "done",
            "configuration": self._configuration(dte),
            "failed_projects": build.LastBuildInfo,
            "errors": sum(d["level"] == "error" for d in diagnostics),
            "warnings": sum(d["level"] == "warning" for d in diagnostics),
            "diagnostics": diagnostics[:MAX_BUILD_DIAGNOSTICS],
            "output_tail": output[-BUILD_OUTPUT_TAIL:],
        }

        if any("LNK1168" in line for line in output):
            payload["hint"] = (
                "LNK1168: the linker could not write a binary because a running process holds it. "
                "Stop the server or client (stop_process) and build again."
            )

        return payload

    def _build_output(self, dte: Com) -> list[str]:
        try:
            pane = dte.Windows.Item(OUTPUT_WINDOW_KIND).Object.OutputWindowPanes.Item("Build")
        except pywintypes.com_error:
            return []

        document = pane.TextDocument
        text = document.StartPoint.CreateEditPoint().GetText(document.EndPoint)
        return text.splitlines()

    def attach(self, pid: int) -> dict[str, Any]:
        debugger = self._dte().Debugger

        for process in debugger.LocalProcesses.items():
            if process.ProcessID == pid:
                process.Attach()
                return self._state(debugger, depth=0)

        raise VsError(f"no local process with pid {pid}")

    def detach(self, wait_s: float = 10.0) -> dict[str, Any]:
        self._dte().Debugger.DetachAll()

        # The old Debugger object fails lookups while detaching, so poll a fresh one
        deadline = time.monotonic() + wait_s
        while True:
            try:
                state = self.state(depth=0)
            except (AttributeError, pywintypes.com_error):
                state = None

            if (state is not None and state["debugger"] == "design") or time.monotonic() > deadline:
                return state or {"debugger": "unknown"}

            time.sleep(POLL_INTERVAL_S)

    def breakpoints(self) -> list[dict[str, Any]]:
        return [
            {
                "file": bp.File,
                "line": bp.FileLine,
                "function": bp.FunctionName,
                "condition": bp.Condition or None,
                "enabled": bool(bp.Enabled),
            }
            for bp in self._dte().Debugger.Breakpoints.items()
        ]

    def add_breakpoint(self, file: str, line: int, condition: str | None) -> list[dict[str, Any]]:
        # Function, File, Line, Column, Condition, ConditionType (dbgBreakpointConditionTypeWhenTrue)
        self._dte().Debugger.Breakpoints.Add("", file, line, 1, condition or "", 1)
        return self.breakpoints()

    def remove_breakpoint(self, file: str, line: int) -> list[dict[str, Any]]:
        removed = 0
        for bp in self._dte().Debugger.Breakpoints.items():
            if bp.FileLine == line and bp.File and _same_file(bp.File, file):
                bp.Delete()
                removed += 1

        if not removed:
            raise VsError(f"no breakpoint at {file}:{line}")

        return self.breakpoints()

    def control(self, action: str, wait_s: float, depth: int) -> dict[str, Any]:
        debugger = self._dte().Debugger
        mode = debugger.CurrentMode

        if mode == DESIGN:
            # Go in design mode would launch the startup project
            raise VsError("Visual Studio is not debugging; vs_attach to a process first")

        if action == "break":
            if mode != BREAK:
                debugger.Break(False)
        elif mode != BREAK:
            raise VsError(f"'{action}' needs the debugger stopped; it is running. Use 'break' first")
        elif action == "continue":
            debugger.Go(False)
        elif action == "step_over":
            debugger.StepOver(False)
        elif action == "step_into":
            debugger.StepInto(False)
        elif action == "step_out":
            debugger.StepOut(False)
        else:
            raise VsError(f"unknown action '{action}'")

        deadline = time.monotonic() + wait_s
        time.sleep(POLL_INTERVAL_S)
        while debugger.CurrentMode == RUN and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)

        return self._state(debugger, depth)

    def state(self, depth: int) -> dict[str, Any]:
        return self._state(self._dte().Debugger, depth)

    def _state(self, debugger: Com, depth: int) -> dict[str, Any]:
        mode = debugger.CurrentMode
        payload: dict[str, Any] = {"debugger": MODES.get(mode, mode)}

        if mode == DESIGN:
            return payload

        process = debugger.CurrentProcess
        if process is not None:
            payload["process"] = {"pid": process.ProcessID, "name": process.Name}

        if mode != BREAK:
            return payload

        reason = debugger.LastBreakReason
        payload["reason"] = BREAK_REASONS.get(reason, reason)

        thread = debugger.CurrentThread
        payload["thread"] = {"id": thread.ID, "name": thread.Name}

        current = debugger.CurrentStackFrame
        current_name = current.FunctionName if current is not None else None
        payload["stack"] = [
            {
                "index": index,
                "function": frame.FunctionName,
                "module": frame.Module,
                "file": _optional(frame, "FileName"),
                "line": _optional(frame, "LineNumber"),
            }
            for index, frame in enumerate(thread.StackFrames.items(MAX_STACK_FRAMES))
        ]

        if current is not None:
            payload["current_frame"] = current_name
            payload["locals"] = [_expression(local, depth) for local in current.Locals.items(MAX_MEMBERS)]

        return payload

    def select_frame(self, index: int, depth: int) -> dict[str, Any]:
        debugger = self._dte().Debugger

        if debugger.CurrentMode != BREAK:
            raise VsError("the debugger is not stopped")

        frames = debugger.CurrentThread.StackFrames
        if not 0 <= index < frames.Count:
            raise VsError(f"frame {index} out of range, the stack has {frames.Count}")

        debugger.CurrentStackFrame = frames.Item(index + 1)
        return self._state(debugger, depth)

    def evaluate(self, expression: str, depth: int) -> dict[str, Any]:
        debugger = self._dte().Debugger

        if debugger.CurrentMode != BREAK:
            raise VsError("expressions can only be evaluated while the debugger is stopped")

        result = debugger.GetExpression(expression, False, 2000)
        if not result.IsValidValue:
            raise VsError(f"cannot evaluate '{expression}': {result.Value}")

        return _expression(result, depth)
