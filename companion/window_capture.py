"""Read-only Windows client-area capture; never restores or repositions windows.

PrintWindow is deliberately the only capture path: screen grabbing an occluded
window would read GSPro's pixels. A minimized/blank window is an actionable error.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path

import numpy as np


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    executable: str = ""


def _win32():
    if os.name != "nt":
        raise CaptureError("FS Golf window capture requires Windows.")
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    signatures = {
        "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        "GetDC": ([wintypes.HWND], wintypes.HDC),
        "ReleaseDC": ([wintypes.HWND, wintypes.HDC], ctypes.c_int),
        "PrintWindow": ([wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
        "IsIconic": ([wintypes.HWND], wintypes.BOOL),
        "IsWindow": ([wintypes.HWND], wintypes.BOOL),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(user, name)
        fn.argtypes, fn.restype = args, result
    for name, args, result in (
        ("CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
        ("CreateCompatibleBitmap", [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
        ("SelectObject", [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
        ("DeleteObject", [wintypes.HGDIOBJ], wintypes.BOOL),
        ("DeleteDC", [wintypes.HDC], wintypes.BOOL),
    ):
        fn = getattr(gdi, name)
        fn.argtypes, fn.restype = args, result
    return user, gdi


def discover_windows(executable: str | None = None, title: str | None = None) -> list[WindowInfo]:
    """Only identify FlightScope windows or an explicitly configured executable.

    An optional title narrows the process match. It cannot match browser pages or
    another app merely because their title contains "FS Golf".
    """
    import psutil

    user, _ = _win32()
    wanted = os.path.normcase(os.path.abspath(executable)) if executable else None
    known_names = {"flightscopevideoteachingapp.exe", "fsgolf.exe", "fsgolfpc.exe", "fs golf.exe"}
    processes = {}
    for process in psutil.process_iter(["pid", "name", "exe"]):
        info = process.info
        path = info.get("exe") or ""
        matches = (os.path.normcase(os.path.abspath(path)) == wanted) if wanted else (
            (info.get("name") or "").lower() in known_names)
        if matches:
            processes[info["pid"]] = path
    results = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in processes:
            return True
        buffer = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
        user.GetWindowTextW(hwnd, buffer, len(buffer))
        if buffer.value and (not title or title.casefold() in buffer.value.casefold()):
            results.append(WindowInfo(int(hwnd), pid.value, buffer.value, processes[pid.value]))
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    if not user.EnumWindows(visit, 0):
        raise CaptureError("The Windows desktop is unavailable. Run Mevo Companion in your signed-in desktop session.")
    # Prefer the main app over its dialogs, without manipulating any windows.
    def area(item):
        rect = wintypes.RECT()
        user.GetClientRect(item.hwnd, ctypes.byref(rect))
        return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
    return sorted(results, key=area, reverse=True)


class WindowCapture:
    def __init__(self, window: WindowInfo):
        self.window = window

    def capture(self) -> np.ndarray:
        """Return RGB pixels in client-area coordinates, releasing every GDI handle."""
        user, gdi = _win32()
        hwnd = self.window.hwnd
        if not user.IsWindow(hwnd):
            raise CaptureError("FS Golf closed. Waiting for it to reopen.")
        if user.IsIconic(hwnd):
            raise CaptureError("Restore FS Golf from the taskbar so its shot screen can update.")
        rect = wintypes.RECT()
        if not user.GetClientRect(hwnd, ctypes.byref(rect)):
            raise CaptureError("FS Golf's window could not be measured.")
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 200 or height < 150 or width * height > 40_000_000:
            raise CaptureError("FS Golf's window is too small or unavailable. Open its shot data screen.")

        class BitmapHeader(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                        ("height", wintypes.LONG), ("planes", wintypes.WORD),
                        ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                        ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG),
                        ("yppm", wintypes.LONG), ("used", wintypes.DWORD),
                        ("important", wintypes.DWORD)]

        dc = user.GetDC(hwnd)
        memory_dc = bitmap = old = None
        try:
            if not dc:
                raise CaptureError("FS Golf's drawing surface is unavailable.")
            memory_dc = gdi.CreateCompatibleDC(dc)
            bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
            if not memory_dc or not bitmap:
                raise CaptureError("Windows could not allocate an FS Golf capture.")
            old = gdi.SelectObject(memory_dc, bitmap)
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT. Does not change z-order/focus.
            if not user.PrintWindow(hwnd, memory_dc, 3):
                raise CaptureError("FS Golf is not exposing its shot screen for capture.")
            # GetDIBits requires the bitmap to be deselected from the DC.
            gdi.SelectObject(memory_dc, old)
            old = None
            header = BitmapHeader(ctypes.sizeof(BitmapHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                     wintypes.UINT, ctypes.c_void_p,
                                     ctypes.POINTER(BitmapHeader), wintypes.UINT]
            gdi.GetDIBits.restype = ctypes.c_int
            if gdi.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0) != height:
                raise CaptureError("Windows returned an incomplete FS Golf capture.")
            bgra = np.frombuffer(buffer, dtype=np.uint8).reshape(height, width, 4)
            rgb = bgra[:, :, 2::-1].copy()
            if float(rgb.std()) < 2:
                raise CaptureError("FS Golf returned a blank image. Open its shot data screen.")
            return rgb
        finally:
            if old and memory_dc:
                gdi.SelectObject(memory_dc, old)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory_dc:
                gdi.DeleteDC(memory_dc)
            if dc:
                user.ReleaseDC(hwnd, dc)
