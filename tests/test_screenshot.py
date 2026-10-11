import asyncio
import json
import zlib

import pytest

pytest.importorskip("win32gui")

from blackice_mcp import screenshot  # noqa: E402
from blackice_mcp.hub import Hub  # noqa: E402
from blackice_mcp.server import McpSurface  # noqa: E402
from blackice_mcp.supervisor import Supervisor  # noqa: E402


def _surface():
    return McpSurface(hub=Hub(host="127.0.0.1", port=0), supervisor=Supervisor(("127.0.0.1", 1), ""))


def test_png_rows_round_trip():
    png = screenshot._png(bytes([1, 2, 3, 4, 5, 6]), 1, 2)

    assert png.startswith(b"\x89PNG")
    idat = png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"\x00\x01\x02\x03\x00\x04\x05\x06"


def test_tool_returns_an_image(monkeypatch):
    monkeypatch.setattr(screenshot, "capture", lambda pid, max_size: b"png-bytes")
    result = asyncio.run(_surface().server.call_tool("screenshot", {"pid": 1}))

    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"


def test_windowless_pid_is_an_error():
    result = asyncio.run(_surface().server.call_tool("screenshot", {"pid": 0}))

    assert "no visible window" in json.loads(result.content[0].text)["error"]
