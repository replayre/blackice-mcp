import json

import pytest

from blackice_mcp.paths import IS_WINDOWS
from blackice_mcp.supervisor import NATIVE_SETUP, UGC_SETUP, load_setup_type


def test_a_missing_file_is_a_ugc_setup(tmp_path):
    assert load_setup_type(tmp_path / "absent.json") == UGC_SETUP


def test_an_unset_type_is_a_ugc_setup(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"profiles": {}}))

    assert load_setup_type(path) == UGC_SETUP


def test_a_native_type_is_read(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"setup_type": "native", "profiles": {}}))

    assert load_setup_type(path) == NATIVE_SETUP


def test_an_unrecognised_type_falls_back_to_ugc(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"setup_type": "whatever"}))

    assert load_setup_type(path) == UGC_SETUP


def test_unreadable_json_falls_back_to_ugc(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("{not json")

    assert load_setup_type(path) == UGC_SETUP


def _surface(setup_type):
    from blackice_mcp.hub import Hub
    from blackice_mcp.server import McpSurface
    from blackice_mcp.supervisor import Supervisor

    return McpSurface(
        hub=Hub(host="127.0.0.1", port=0),
        supervisor=Supervisor(("127.0.0.1", 1), ""),
        setup_type=setup_type,
    )


def _tool_names(setup_type):
    import asyncio

    tools = asyncio.run(_surface(setup_type).server.list_tools())

    return {tool.name for tool in tools}


def test_the_vs_tools_are_not_offered_by_default():
    assert not [name for name in _tool_names(UGC_SETUP) if name.startswith("vs_")]


@pytest.mark.skipif(not IS_WINDOWS, reason="the vs tools are Windows only")
def test_the_vs_tools_are_offered_on_a_native_setup():
    names = _tool_names(NATIVE_SETUP)

    assert "vs_build" in names
    assert "vs_init" in names


def test_an_agent_is_not_told_about_the_vs_tools_by_default():
    instructions = _surface(UGC_SETUP).server.instructions

    assert "vs_" not in instructions


@pytest.mark.skipif(not IS_WINDOWS, reason="the vs tools are Windows only")
def test_an_agent_is_told_about_the_vs_tools_on_a_native_setup():
    assert "vs_build" in _surface(NATIVE_SETUP).server.instructions
