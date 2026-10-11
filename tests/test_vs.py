import time
from types import SimpleNamespace

import pytest

pytest.importorskip("win32com")

from blackice_mcp import vs as vs_module
from blackice_mcp.vs import (
    BUILD_DONE,
    BUILD_IN_PROGRESS,
    DESIGN,
    SOLUTION_FOLDER_KIND,
    VisualStudio,
    VsError,
)


class FakeSolutionBuild:
    def __init__(self):
        self.BuildState = BUILD_DONE
        self.calls: list[tuple] = []

    def BuildProject(self, configuration, unique_name, wait_for_finish):
        self.calls.append((configuration, unique_name, wait_for_finish))
        self.BuildState = BUILD_IN_PROGRESS

    def Build(self, wait_for_finish):
        self.calls.append(("solution", wait_for_finish))
        self.BuildState = BUILD_IN_PROGRESS


class FakeDte:
    def __init__(self):
        self.Solution = type("Solution", (), {"SolutionBuild": FakeSolutionBuild()})()
        self.Debugger = type("Debugger", (), {"CurrentMode": DESIGN})()


class FakeItems:
    """Counts how often the solution is walked - each call is a COM round trip."""

    def __init__(self, items, walks):
        self._items = items
        self._walks = walks

    def items(self):
        self._walks.append(1)
        return list(self._items)


def test_the_project_lookup_walks_the_solution_only_once(monkeypatch):
    studio = VisualStudio()
    walks: list[int] = []

    project = SimpleNamespace(
        Name="client_cef",
        UniqueName="src\\client_cef.vcxproj",
        Kind="{normal-project}",
        ProjectItems=FakeItems([], walks),
    )
    folder = SimpleNamespace(
        Name="client",
        UniqueName="client",
        Kind=SOLUTION_FOLDER_KIND,
        ProjectItems=FakeItems([SimpleNamespace(SubProject=project)], walks),
    )
    dte = SimpleNamespace(
        Solution=SimpleNamespace(
            FullName="C:\\x\\BlackICE.sln", Projects=FakeItems([folder], walks)
        )
    )

    assert studio._project_unique_name(dte, "client_cef") == "src\\client_cef.vcxproj"
    assert len(walks) == 2  # the top level, then the solution folder

    # Case-insensitively, and without walking again: the solution does not change mid-session
    assert studio._project_unique_name(dte, "CLIENT_CEF") == "src\\client_cef.vcxproj"
    assert len(walks) == 2


def test_an_unknown_project_is_still_reported(monkeypatch):
    studio = VisualStudio()
    walks: list[int] = []
    dte = SimpleNamespace(
        Solution=SimpleNamespace(FullName="C:\\x\\BlackICE.sln", Projects=FakeItems([], walks))
    )

    with pytest.raises(VsError, match="no project 'nope'"):
        studio._project_unique_name(dte, "nope")


def test_init_warms_the_project_index(monkeypatch):
    studio = VisualStudio()
    walks: list[int] = []

    project = SimpleNamespace(
        Name="client_cef",
        UniqueName="src\\client_cef.vcxproj",
        Kind="{normal-project}",
        ProjectItems=FakeItems([], walks),
    )
    dte = SimpleNamespace(
        Solution=SimpleNamespace(
            FullName="C:\\x\\BlackICE.sln", Projects=FakeItems([project], walks)
        )
    )

    monkeypatch.setattr(studio, "_dte", lambda: dte)
    monkeypatch.setattr(studio, "_configuration", lambda dte: "debug|x64")

    assert studio.init() == {
        "solution": "C:\\x\\BlackICE.sln",
        "configuration": "debug|x64",
        "projects": 1,
    }
    assert len(walks) == 1

    # The point of init: the lookup that follows is served without touching COM again
    assert studio._project_unique_name(dte, "client_cef") == "src\\client_cef.vcxproj"
    assert len(walks) == 1


def _studio(monkeypatch, max_wait):
    studio = VisualStudio()
    dte = FakeDte()

    monkeypatch.setattr(studio, "_dte", lambda: dte)
    monkeypatch.setattr(studio, "_configuration", lambda dte: "debug|x64")
    monkeypatch.setattr(studio, "_project_unique_name", lambda dte, name: name)
    monkeypatch.setattr(studio, "_build_status", lambda dte: {"state": "in_progress"})
    monkeypatch.setattr(vs_module, "MAX_BUILD_WAIT_S", max_wait)

    return studio, dte


def test_build_starts_the_project_asynchronously(monkeypatch):
    studio, dte = _studio(monkeypatch, 0.05)

    studio.build("client_cef", 600)

    assert dte.Solution.SolutionBuild.calls == [("debug|x64", "client_cef", False)]


def test_a_long_wait_is_clamped_so_the_call_returns_inside_the_request_timeout(monkeypatch):
    studio, _ = _studio(monkeypatch, 0.05)

    started = time.monotonic()
    result = studio.build("client_cef", 600)
    elapsed = time.monotonic() - started

    assert result == {"state": "in_progress"}
    # The requested 600s would have hung the call far past the transport's request timeout.
    assert elapsed < 5
