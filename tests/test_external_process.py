import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil
import pytest

from companion import external_process as external
from companion.platform_windows import launch_app
from companion.putting_adapter import PuttingAdapter


def test_sanitizes_only_own_frozen_paths_and_preserves_original_environment(tmp_path):
    bundle = tmp_path / "engine"
    separate = tmp_path / "engine-other"
    source = {"PATH": os.pathsep.join([str(bundle), str(bundle / "Qt"), str(separate)]),
              "_MEIPASS2": str(bundle), "_PYI_APPLICATION_HOME_DIR": str(bundle),
              "_PYI_PARENT_PROCESS_LEVEL": "1", "_PYI_SPLASH_IPC": "123",
              "QT_PLUGIN_PATH": str(bundle / "Qt"), "PYTHONHOME": str(bundle),
              "PYTHONPATH": os.pathsep.join([str(bundle), str(separate)]),
              "MY_SETTING": "keep"}
    before = source.copy()
    with patch.object(sys, "_MEIPASS", str(bundle), create=True):
        cleaned = external.child_environment(source)
    assert source == before
    assert cleaned["PATH"] == cleaned["PYTHONPATH"] == str(separate)
    assert cleaned["MY_SETTING"] == "keep"
    assert cleaned["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert not any(key.startswith("_PYI_") for key in cleaned)
    assert not {"_MEIPASS2", "QT_PLUGIN_PATH", "PYTHONHOME"}.intersection(cleaned)


class DllKernel:
    def __init__(self):
        self.directory = "engine-runtime"
        self.changes = []

    def GetDllDirectoryW(self, size, buffer):
        if not size:
            return len(self.directory) + 1
        buffer.value = self.directory
        return len(self.directory)

    def SetDllDirectoryW(self, value):
        self.directory = value
        self.changes.append(value)
        return True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL search")
def test_failed_launch_restores_parent_dll_directory(tmp_path):
    kernel = DllKernel()
    with patch.object(sys, "frozen", True, create=True), patch.object(external, "_dll_kernel", return_value=kernel):
        with patch.object(external.subprocess, "Popen", side_effect=OSError("missing executable")):
            with pytest.raises(OSError, match="missing executable"):
                external.launch_external(["missing.exe"], tmp_path)
    assert kernel.changes == [None, "engine-runtime"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL search")
def test_concurrent_launches_serialize_global_dll_search_changes(tmp_path):
    kernel, seen, failures = DllKernel(), [], []

    def popen(command, **kwargs):
        seen.append(kernel.directory)
        time.sleep(0.03)
        seen.append(kernel.directory)
        return object()

    def launch():
        try:
            external.launch_external(["application.exe"], tmp_path)
        except BaseException as exc:
            failures.append(exc)

    with patch.object(sys, "frozen", True, create=True), patch.object(external, "_dll_kernel", return_value=kernel):
        with patch.object(external.subprocess, "Popen", side_effect=popen):
            threads = [threading.Thread(target=launch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
    assert not failures
    assert seen == [None] * 4
    assert kernel.changes == [None, "engine-runtime", None, "engine-runtime"]


def test_launch_app_uses_shared_sanitized_environment_but_never_a_tracker_job(tmp_path):
    app = tmp_path / "GSPro.exe"
    app.touch()
    with patch.dict(os.environ, {"_MEIPASS2": "old-runtime"}), patch.object(external.subprocess, "Popen") as popen:
        with patch.object(external.OwnedProcessJob, "try_attach") as attach:
            launch_app(str(app))
    assert popen.call_args.args[0] == [str(app)]
    assert popen.call_args.kwargs["cwd"] == str(tmp_path)
    assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert "_MEIPASS2" not in popen.call_args.kwargs["env"]
    attach.assert_not_called()


def test_explicit_helper_private_stdin_pipe_is_preserved(tmp_path):
    with patch.object(external.subprocess, "Popen") as popen:
        external.launch_external(["private-helper.exe"], tmp_path, stdin=subprocess.PIPE)
    assert popen.call_args.kwargs["stdin"] == subprocess.PIPE


class JobKernel:
    def __init__(self, *, assign=True, configure=True):
        self.assign, self.configure = assign, configure
        self.assigned, self.closed = [], []
        self.handle = 0x123456789ABC

    def CreateJobObjectW(self, security, name):
        assert security is None and name is None  # Private, unnamed, not inheritable.
        return self.handle

    def SetInformationJobObject(self, handle, info_class, data, size):
        assert handle == self.handle and info_class == 9
        limits = ctypes.cast(data, ctypes.POINTER(external._ExtendedLimitInformation)).contents
        assert limits.BasicLimitInformation.LimitFlags == 0x2000
        assert limits.BasicLimitInformation.ActiveProcessLimit == 0
        return self.configure

    def AssignProcessToJobObject(self, job, process):
        self.assigned.append((job, process))
        return self.assign

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_job_uses_exact_owned_handle_and_closes_only_once():
    kernel = JobKernel()
    process = SimpleNamespace(_handle=0x987654321ABC)
    with patch.object(external, "_job_kernel", return_value=kernel):
        job = external.OwnedProcessJob(process)
        assert kernel.assigned == [(kernel.handle, process._handle)]
        job.close()
        job.close()
    assert kernel.closed == [kernel.handle]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
@pytest.mark.parametrize("configure,assign", [(False, True), (True, False)])
def test_incompatible_job_policy_falls_back_without_killing_owned_process(configure, assign, caplog):
    kernel = JobKernel(configure=configure, assign=assign)
    process = Mock(_handle=0x987654321ABC)
    with patch.object(external, "_job_kernel", return_value=kernel):
        assert external.OwnedProcessJob.try_attach(process) is None
    assert kernel.closed == [kernel.handle]
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    assert "normal shutdown cleanup remains enabled" in caplog.text


def test_putting_closes_job_even_when_bootloader_already_exited():
    job = Mock()
    adapter = SimpleNamespace(_process=Mock(poll=Mock(return_value=0)), _process_job=job,
                              _window_state=(True, True), _view_available=True)
    PuttingAdapter._stop_owned_process(adapter)
    job.close.assert_called_once()
    assert adapter._process is None and adapter._process_job is None
    assert adapter._window_state is None and not adapter._view_available


def stop(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, name)
        if stream:
            stream.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows Job Object integration")
def test_real_job_close_stops_only_assigned_child(tmp_path):
    children = [external.launch_external([sys.executable, "-c", "import time;time.sleep(30)"], tmp_path)
                for _ in range(2)]
    try:
        job = external.OwnedProcessJob(children[0])
        job.close()
        assert children[0].wait(timeout=5) is not None
        assert children[1].poll() is None
    finally:
        for process in children:
            stop(process)


@pytest.mark.skipif(sys.platform != "win32", reason="Actual parent-crash/nested-job integration")
def test_forced_parent_crash_releases_nested_job_child(tmp_path):
    # A stand-in for the engine, with no camera or user application involved.
    script = """
import json, subprocess, sys
from companion.external_process import OwnedProcessJob
sys.stdin.readline()
child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
job = OwnedProcessJob(child)
print(json.dumps({'pid': child.pid}), flush=True)
sys.stdin.read()
"""
    owner = external.launch_external([sys.executable, "-u", "-c", script], Path(__file__).resolve().parents[1],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    outer_job = child = None
    try:
        # Simulates hosts that already supervise the engine in another job.
        outer_job = external.OwnedProcessJob(owner)
        owner.stdin.write("start\n")
        owner.stdin.flush()
        line = owner.stdout.readline()
        assert line, owner.stderr.read()
        child = psutil.Process(json.loads(line)["pid"])
        owner.kill()  # No Python finally, stop(), or application cleanup runs.
        owner.wait(timeout=5)
        child.wait(timeout=5)
        assert not child.is_running()
    finally:
        stop(owner)
        if outer_job:
            outer_job.close()
        if child and child.is_running():
            child.kill()
            child.wait(timeout=5)
