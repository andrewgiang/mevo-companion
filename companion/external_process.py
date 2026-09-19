"""Start independent applications without leaking the frozen engine's runtime.

Only the stock putting tracker is assigned an owned job. GSPro and FS Golf must
remain independent of the companion's lifetime.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading


LOG = logging.getLogger(__name__)
_DLL_LAUNCH_LOCK = threading.Lock()


def child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy; retain user settings and remove only our frozen paths."""
    environment = dict(os.environ if source is None else source)
    for key in tuple(environment):
        if key == "_MEIPASS2" or key.startswith("_PYI_"):
            environment.pop(key)
    # A separate PyInstaller executable must unpack/init its own environment.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        root = os.path.normcase(os.path.abspath(frozen_root))

        def is_bundled(value: str) -> bool:
            if not value:
                return False
            value = os.path.expandvars(value.strip('"'))
            try:
                return os.path.commonpath([root, os.path.normcase(os.path.abspath(value))]) == root
            except ValueError:  # Different Windows drives.
                return False

        for key in ("PATH", "PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                    "QML2_IMPORT_PATH", "QML_IMPORT_PATH"):
            if key in environment:
                values = [value for value in environment[key].split(os.pathsep) if not is_bundled(value)]
                if values or key == "PATH":
                    environment[key] = os.pathsep.join(values)
                else:
                    environment.pop(key)
        if is_bundled(environment.get("PYTHONHOME", "")):
            environment.pop("PYTHONHOME", None)
    return environment


def _dll_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    kernel.GetDllDirectoryW.restype = wintypes.DWORD
    kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel.SetDllDirectoryW.restype = wintypes.BOOL
    return kernel


@contextmanager
def _external_dll_search():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        yield
        return
    # SetDllDirectory is process-wide. All independent launches share this lock;
    # the parent's extension search path is restored even if Popen fails.
    with _DLL_LAUNCH_LOCK:
        kernel = _dll_kernel()
        ctypes.set_last_error(0)
        size = kernel.GetDllDirectoryW(0, None)
        if not size and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size + 1)
        if size:
            ctypes.set_last_error(0)
            read = kernel.GetDllDirectoryW(len(buffer), buffer)
            if (not read and ctypes.get_last_error()) or read >= len(buffer):
                raise OSError("Could not preserve the engine's Windows DLL search path")
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not kernel.SetDllDirectoryW(buffer.value or None):
                # Popen may already have succeeded: retain its handle for cleanup
                # rather than throwing here and leaving an untracked child.
                LOG.error("Could not restore Windows DLL search path: %s", ctypes.WinError(ctypes.get_last_error()))


def launch_external(command: list[str], work_dir: str | Path, **kwargs) -> subprocess.Popen:
    """Launch an independent executable, retaining explicitly supplied streams."""
    kwargs["cwd"] = str(Path(work_dir).resolve(strict=True))
    kwargs["env"] = child_environment(kwargs.pop("env", None))
    kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # stdin belongs to the desktop/engine command protocol. Independent programs
    # must not inherit that pipe; a supervised helper can request its own PIPE.
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    with _external_dll_search():
        return subprocess.Popen(command, **kwargs)


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimitInformation), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _job_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


class OwnedProcessJob:
    """An unnamed, non-inherited job containing only the supplied owned child.

    Windows 8+ permits nested jobs; no breakaway, UI, memory, or process-count
    limits are imposed. Attach immediately after Popen, before the tracker can
    unpack and start its Python child. The tiny Popen-to-attach interval remains
    unprotected; once attached, a forced parent exit also releases the camera.
    """
    def __init__(self, process: subprocess.Popen):
        self._handle = None
        self._kernel = _job_kernel()
        handle = self._kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self._kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            # Use the Popen handle, never a PID search/name match that might target
            # another copy of Springbok or a user-owned application.
            if not self._kernel.AssignProcessToJobObject(handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    @classmethod
    def try_attach(cls, process: subprocess.Popen) -> OwnedProcessJob | None:
        if sys.platform != "win32":
            return None
        try:
            return cls(process)
        except (OSError, AttributeError, TypeError, ValueError):
            # Some host job policies disallow nesting. Keep the working camera
            # and ordinary owned-process cleanup instead of blocking startup.
            LOG.warning("Stock putting crash cleanup unavailable; normal shutdown cleanup remains enabled", exc_info=True)
            return None

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            if not self._kernel.CloseHandle(handle):
                LOG.error("Could not close the owned putting job: %s", ctypes.WinError(ctypes.get_last_error()))

    def __del__(self):
        self.close()
