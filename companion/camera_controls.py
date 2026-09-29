"""Read and preserve a webcam's exposure mode for the stock putting tracker.

OpenCV's DirectShow backend can set CAP_PROP_AUTO_EXPOSURE but cannot read it,
so the unmodified tracker saves ``autoexposure = -1.0`` and replays it at launch,
which DirectShow treats as "switch to manual exposure". The camera's own mode is
read here through IAMCameraControl, without opening a video stream.
"""
from __future__ import annotations

from configparser import ConfigParser, Error as ConfigError
import ctypes
import logging
import os
from pathlib import Path
import re
import sys
import threading
from typing import Callable


LOG = logging.getLogger(__name__)
CAMERA_CONTROL_EXPOSURE = 4
CAMERA_CONTROL_FLAGS_AUTO = 0x1
S_OK = 0
VT_BSTR = 8
AUTO_EXPOSURE_OPTION = re.compile(r"^(\s*autoexposure\s*[=:]\s*)", re.IGNORECASE)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class VARIANT(ctypes.Structure):
    # 16 bytes on 32-bit and 24 bytes on 64-bit Windows, like oaidl.h.
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                ("value", ctypes.c_void_p), ("record", ctypes.c_void_p)]


def _vtable(interface: ctypes.c_void_p):
    return ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents


def _method(interface: ctypes.c_void_p, index: int, *argtypes):
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(_vtable(interface)[index])


def _release(interface: ctypes.c_void_p) -> None:
    if interface:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(_vtable(interface)[2])(interface)


def _read_auto_exposure(matches: Callable[[str], bool]) -> bool | None:
    ole32, oleaut32 = ctypes.OleDLL("ole32"), ctypes.WinDLL("oleaut32")

    def guid(text: str) -> GUID:
        value = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value))
        return value

    clsid_device_enum = guid("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
    iid_create_dev_enum = guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
    video_input_category = guid("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")
    iid_property_bag = guid("{55272A00-42CB-11CE-8135-00AA004BB851}")
    iid_base_filter = guid("{56A86895-0AD4-11CE-B03A-0020AF0BA770}")
    iid_camera_control = guid("{C6E13370-30AC-11D0-A18C-00A0C9118956}")
    pointer = ctypes.c_void_p

    ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED on this private thread.
    device_enum, monikers = pointer(), pointer()
    try:
        ole32.CoCreateInstance(ctypes.byref(clsid_device_enum), None, 1,  # CLSCTX_INPROC_SERVER
                               ctypes.byref(iid_create_dev_enum), ctypes.byref(device_enum))
        # ICreateDevEnum::CreateClassEnumerator returns S_FALSE and no enumerator for an empty category.
        if _method(device_enum, 3, pointer, pointer, ctypes.c_ulong)(
                device_enum, ctypes.byref(video_input_category), ctypes.byref(monikers), 0) != S_OK:
            return None
        next_moniker = _method(monikers, 3, ctypes.c_ulong, pointer, pointer)  # IEnumMoniker::Next
        while True:
            moniker = pointer()
            if next_moniker(monikers, 1, ctypes.byref(moniker), None) != S_OK:
                return None
            bag, capture_filter, control = pointer(), pointer(), pointer()
            try:
                # IMoniker::BindToStorage, then IPropertyBag::Read.
                if _method(moniker, 9, pointer, pointer, pointer, pointer)(
                        moniker, None, None, ctypes.byref(iid_property_bag), ctypes.byref(bag)) != S_OK:
                    continue
                variant = VARIANT()
                if _method(bag, 3, ctypes.c_wchar_p, pointer, pointer)(
                        bag, "DevicePath", ctypes.byref(variant), None) != S_OK:
                    continue
                try:
                    device_path = ctypes.wstring_at(variant.value) if variant.vt == VT_BSTR and variant.value else ""
                finally:
                    oleaut32.VariantClear(ctypes.byref(variant))
                if not device_path or not matches(device_path):
                    continue
                # IMoniker::BindToObject creates the filter without building a graph
                # or streaming; IAMCameraControl::Get is read-only.
                if _method(moniker, 8, pointer, pointer, pointer, pointer)(
                        moniker, None, None, ctypes.byref(iid_base_filter), ctypes.byref(capture_filter)) != S_OK:
                    return None
                if _method(capture_filter, 0, pointer, pointer)(
                        capture_filter, ctypes.byref(iid_camera_control), ctypes.byref(control)) != S_OK:
                    return None
                value, flags = ctypes.c_long(), ctypes.c_long()
                if _method(control, 5, ctypes.c_long, pointer, pointer)(
                        control, CAMERA_CONTROL_EXPOSURE, ctypes.byref(value), ctypes.byref(flags)) != S_OK:
                    return None
                return bool(flags.value & CAMERA_CONTROL_FLAGS_AUTO)
            finally:
                for interface in (control, capture_filter, bag, moniker):
                    _release(interface)
    finally:
        _release(monikers)
        _release(device_enum)
        ole32.CoUninitialize()


def directshow_auto_exposure(matches: Callable[[str], bool], timeout: float = 3.0) -> bool | None:
    """Return whether the matching DirectShow camera is on auto exposure.

    None means unknown: not Windows, no matching device, no exposure control,
    or the driver did not answer in time. Callers must then change nothing.
    """
    if sys.platform != "win32":
        return None
    result = []

    def worker():
        try:
            result.append(_read_auto_exposure(matches))
        except (OSError, ValueError, ctypes.ArgumentError) as exc:
            LOG.info("Could not read the putting camera's exposure mode: %s", exc)

    thread = threading.Thread(target=worker, daemon=True, name="PuttingCameraExposure")
    thread.start()
    thread.join(timeout)
    return result[0] if result else None


def sync_stock_auto_exposure(config_path: str | Path, auto: bool) -> bool:
    """Make the stock tracker's DirectShow startup restore ``auto``.

    Only the [putting] ``autoexposure`` line changes. OpenCV 4.7 DirectShow
    enables auto exposure only for a value that rounds to 1; a missing key
    replays -1. Returns True when the file was rewritten.
    """
    path = Path(config_path)
    parser = ConfigParser()
    try:
        if not parser.read(path, encoding="utf-8") or not parser.has_section("putting"):
            return False
        current = parser.getfloat("putting", "autoexposure", fallback=-1.0)
    except (ConfigError, ValueError, UnicodeError):
        return False
    if (round(current) == 1) == auto:
        return False
    wanted = "1.0" if auto else "-1.0"
    lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)  # Keep the stock \r\n endings.
    newline = next((line[len(line.rstrip("\r\n")):] for line in lines if line.endswith("\n")), "\n")
    section, header = None, None
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if body.strip().startswith("["):
            section = body.strip().strip("[]").strip()
            if section == "putting":
                header = index
        elif section == "putting" and (match := AUTO_EXPOSURE_OPTION.match(body)):
            lines[index] = match.group(1) + wanted + line[len(body):]
            break
    else:
        if not lines[header].endswith("\n"):
            lines[header] += newline
        lines.insert(header + 1, f"autoexposure = {wanted}{newline}")
    temporary = path.with_name(path.name + ".exposure.tmp")
    temporary.write_text("".join(lines), encoding="utf-8", newline="")
    os.replace(temporary, path)
    return True
