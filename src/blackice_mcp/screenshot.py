from __future__ import annotations

import ctypes
import struct
import zlib

import win32gui
import win32process
import win32ui

# PW_CLIENTONLY | PW_RENDERFULLCONTENT: the client area, including DirectX content via DWM
PRINT_FLAGS = 1 | 2
DPI_AWARENESS_PER_MONITOR_V2 = -4


def _window_for(pid: int) -> int:
    found: list[tuple[int, int]] = []

    def visit(hwnd: int, _: object) -> bool:
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            _, _, w, h = win32gui.GetClientRect(hwnd)
            found.append((w * h, hwnd))
        return True

    win32gui.EnumWindows(visit, None)
    if not found:
        raise RuntimeError(f"pid {pid} has no visible window")
    return max(found)[1]


def _png(rgb: bytes, width: int, height: int) -> bytes:
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def capture(pid: int, max_size: int) -> bytes:
    """PNG of a process's largest visible window. Exclusive fullscreen comes back black."""
    # Otherwise a DPI-aware game reports scaled-down client bounds and the capture is cropped
    ctypes.windll.user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_PER_MONITOR_V2)

    hwnd = _window_for(pid)
    _, _, width, height = win32gui.GetClientRect(hwnd)
    if width == 0 or height == 0:
        raise RuntimeError(f"pid {pid}'s window is minimised")

    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    memory.SelectObject(bitmap)

    try:
        if not ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), PRINT_FLAGS):
            raise RuntimeError(f"PrintWindow failed for pid {pid}")
        bgra = bitmap.GetBitmapBits(True)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)

    step = max(1, -(-max(width, height) // max_size))
    rows = [bgra[y * width * 4 : (y + 1) * width * 4] for y in range(0, height, step)]
    out_w = len(range(0, width, step))
    rgb = bytearray(out_w * 3 * len(rows))

    for i, row in enumerate(rows):
        base = i * out_w * 3
        rgb[base : base + out_w * 3 : 3] = row[2 :: 4 * step]
        rgb[base + 1 : base + out_w * 3 : 3] = row[1 :: 4 * step]
        rgb[base + 2 : base + out_w * 3 : 3] = row[0 :: 4 * step]

    return _png(bytes(rgb), out_w, len(rows))
