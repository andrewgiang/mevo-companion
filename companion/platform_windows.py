"""Small Windows integration layer; never terminates user-owned applications."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

import psutil

from .external_process import launch_external


@dataclass(frozen=True)
class InstalledApps:
    gspro: str = ""
    fs_golf: str = ""


def processes() -> list[dict]:
    result = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            result.append(proc.info)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return result


def gspro_running(items: list[dict] | None = None) -> bool:
    return any((p.get("name") or "").lower() in
               {"gspro.exe", "gsprolauncher.exe", "gsplauncher.exe", "gspconnect.exe",
                "gsproconnect.exe", "apiv1 connect.exe", "gspro launch.exe"}
               for p in (items if items is not None else processes()))


def fs_golf_running(items: list[dict] | None = None) -> bool:
    return any(any(x in (p.get("name") or "").lower() for x in ("fsgolf", "fs golf", "fsmevo", "flightscopevideoteachingapp"))
               for p in (items if items is not None else processes()))


def conflicting_connectors(items: list[dict] | None = None) -> list[str]:
    patterns = ("mlm2pro-gspro", "ball_tracking", "mevoputt", "shotbridge", "flightscopegspro")
    return sorted({p["name"] for p in (items if items is not None else processes())
                   if any(x in (p.get("name") or "").lower() for x in patterns)})


def discover_apps() -> InstalledApps:
    gspro, golf = "", ""
    for p in processes():
        name = (p.get("name") or "").lower()
        if p.get("exe"):
            if name in {"gsprolauncher.exe", "gsplauncher.exe", "gspro.exe"}:
                gspro = p["exe"]
            if any(x in name for x in ("fsgolf", "fs golf", "fsmevo", "flightscopevideoteachingapp")):
                golf = p["exe"]
    roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
             Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")), Path("C:/")]
    candidates = []
    for base in roots:
        candidates.extend([base / "GSProV1" / "GSPLauncher.exe",
                           base / "GSPro" / "GSPLauncher.exe",
                           base / "GSPro" / "GSProLauncher.exe", base / "GSPro" / "GSPro.exe",
                           base / "GSProV1" / "Core" / "GSP" / "GSPro.exe"])
    if not gspro:
        gspro = next((str(p) for p in candidates if p.is_file()), "")
    if not golf:
        for base in roots[:2]:
            folder = base / "FlightScope" / "FS Golf PC 2.0"
            if folder.is_dir():
                for p in folder.glob("*.exe"):
                    if any(token in p.stem.lower() for token in ("fsgolf", "fs golf", "fsmevo", "flightscopevideoteachingapp")):
                        golf = str(p)
                        break
    if sys.platform == "win32" and not gspro:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for branch in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                           r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(hive, branch) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                                    name = winreg.QueryValueEx(app, "DisplayName")[0]
                                    if "gspro" not in str(name).lower():
                                        continue
                                    location = winreg.QueryValueEx(app, "InstallLocation")[0]
                                    for filename in ("GSPLauncher.exe", "GSProLauncher.exe", "GSPro.exe"):
                                        path = Path(location) / filename
                                        if path.is_file():
                                            gspro = str(path)
                            except OSError:
                                pass
                except OSError:
                    pass
    return InstalledApps(gspro, golf)


def launch_app(path: str) -> subprocess.Popen:
    executable = Path(path)
    if not executable.is_file() or executable.suffix.lower() != ".exe":
        raise ValueError("Choose the application's installed .exe file")
    executable = executable.resolve()
    return launch_external([str(executable)], executable.parent)


def set_startup(enabled: bool) -> None:
    if sys.platform != "win32":
        raise OSError("Windows startup is available on Windows only")
    import winreg
    if getattr(sys, "frozen", False):
        command = subprocess.list2cmdline([sys.executable, "--background"])
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        entry = Path(__file__).resolve().parent.parent / "MevoCompanion.py"
        command = subprocess.list2cmdline([str(pythonw), str(entry), "--background"])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "MevoCompanion", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "MevoCompanion")
            except FileNotFoundError:
                pass
