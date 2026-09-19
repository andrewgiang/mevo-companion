"""Collect actual runtime license texts into a deterministic package directory.

Call ``collect(package / 'licenses')`` after publishing the native desktop app,
using the same Python environment that built the engine. No network requests are
made. Pinned supplemental notices are refreshed separately by the packaging tool.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DISTRIBUTIONS = (
    "PySide6", "PySide6-Essentials", "shiboken6", "opencv-python-headless",
    "numpy", "Pillow", "psutil", "tesserocr", "websocket-client", "cv2-enumerate-cameras",
)
DOTNET_PACKAGES = (
    "microsoft.netcore.app.runtime.win-x64",
    "microsoft.windowsdesktop.app.runtime.win-x64",
)


def _safe_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _license_name(path: Path) -> bool:
    name = path.name.upper()
    return (name.startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE", "THIRD-PARTY", "THIRDPARTY"))
            and path.suffix.lower() in {"", ".txt", ".md", ".rst"})


def _version_key(value: str):
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"[.-]", value))


class _Collector:
    def __init__(self, destination: Path):
        self.destination = destination.resolve()
        self.destination.mkdir(parents=True, exist_ok=True)
        self.files: dict[str, dict] = {}
        self.components: list[dict] = []

    def copy(self, source: Path, relative: str, provenance: str) -> None:
        target = (self.destination / relative).resolve()
        if not target.is_relative_to(self.destination) or target == self.destination:
            raise ValueError("License destination must remain inside the package")
        data = source.read_bytes()
        if not data:
            raise ValueError(f"Empty runtime license: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        key = target.relative_to(self.destination).as_posix()
        record = {"path": key, "sha256": hashlib.sha256(data).hexdigest(), "source": provenance}
        if key in self.files and self.files[key] != record:
            raise ValueError(f"Conflicting runtime notices for {key}")
        self.files[key] = record

    def distribution(self, name: str, *, runtime_note: str | None = None) -> None:
        distribution = metadata.distribution(name)
        normalized = _safe_name(distribution.metadata.get("Name", name))
        files = list(distribution.files or [])
        declarations = set(distribution.metadata.get_all("License-File") or [])
        selected = [file for file in files if _license_name(Path(str(file))) or
                    any(str(file).replace("\\", "/").endswith("/" + license_file) for license_file in declarations)]
        if not selected:
            raise RuntimeError(f"No installed license text was found for {name} {distribution.version}")
        component = {"name": normalized, "version": distribution.version, "kind": "python-runtime"}
        if runtime_note:
            component["note"] = runtime_note
        self.components.append(component)
        for file in sorted(selected, key=str):
            relative = Path(str(file))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unexpected license path in {name} wheel: {file}")
            self.copy(Path(distribution.locate_file(file)),
                      f"python-packages/{normalized}-{distribution.version}/{relative.as_posix()}",
                      f"installed-wheel:{normalized}/{distribution.version}/{relative.as_posix()}")

    def python_runtime(self) -> None:
        runtime = Path(sys.base_prefix)
        candidates = [runtime / "LICENSE.txt", runtime / "LICENSE", runtime / "share/doc/python" / "LICENSE"]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            raise RuntimeError(f"Python's runtime license was not found under {runtime}")
        version = platform.python_version()
        self.components.append({"name": "cpython", "version": version, "kind": "native-runtime"})
        self.copy(source, f"python-{version}/LICENSE.txt", f"python-runtime:{version}/LICENSE.txt")

    def supplemental(self) -> None:
        base = ROOT / "packaging/license-notices"
        manifest_path = base / "sources.json"
        source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if source_manifest.get("missing"):
            raise RuntimeError("Supplemental runtime notices are incomplete; refresh packaging/fetch-license-notices.py")
        groups = set()
        for record in source_manifest["files"]:
            source = (base / record["file"]).resolve()
            if not source.is_relative_to(base.resolve()):
                raise ValueError("Unsafe supplemental notice path")
            if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
                raise RuntimeError(f"Supplemental notice hash mismatch: {record['file']}")
            groups.add(Path(record["file"]).parts[0])
            self.copy(source, "native-libraries/" + record["file"], record["url"])
        for group in sorted(groups):
            self.components.append({"name": group, "kind": "native-library-notices"})
        self.copy(manifest_path, "native-libraries/sources.json", "repository:packaging/license-notices/sources.json")

    def dotnet(self) -> None:
        caches = []
        versions: dict[str, str] = {}
        # Prefer the packages selected by the actual desktop publish. An SDK can
        # contain a different patch version from the self-contained output.
        assets_path = ROOT / "desktop/MevoCompanion/obj/project.assets.json"
        if assets_path.is_file():
            assets = json.loads(assets_path.read_text(encoding="utf-8"))
            caches.extend(Path(folder) for folder in assets.get("packageFolders", {}))
            for library in assets.get("libraries", {}):
                package, _, version = library.lower().partition("/")
                if package in DOTNET_PACKAGES:
                    versions[package] = version
        if os.environ.get("NUGET_PACKAGES"):
            caches.append(Path(os.environ["NUGET_PACKAGES"]))
        caches.extend([ROOT / ".tooling/nuget", ROOT / ".tooling/installer-nuget",
                       Path.home() / ".nuget/packages"])
        for name in DOTNET_PACKAGES:
            candidates = [path for cache in caches for path in (cache / name).glob("*") if path.is_dir()]
            if name in versions:
                candidates = [path for path in candidates if path.name == versions[name]]
            if not candidates:
                raise RuntimeError(f"Publish the native desktop app first; {name} runtime notices are missing")
            source = max(candidates, key=lambda path: _version_key(path.name))
            notices = sorted(path for path in source.iterdir() if path.is_file() and _license_name(path))
            if not notices:
                raise RuntimeError(f"No actual license text in {name} {source.name}")
            self.components.append({"name": name, "version": source.name, "kind": "dotnet-runtime"})
            for notice in notices:
                self.copy(notice, f"dotnet/{name}-{source.name}/{notice.name}", f"nuget:{name}/{source.name}/{notice.name}")
        sdk = ROOT / ".tooling/dotnet"
        for name in ("LICENSE.txt", "ThirdPartyNotices.txt"):
            self.copy(sdk / name, "dotnet/shared-notices/" + name, "dotnet-sdk-distribution:" + name)
        # The WindowsDesktop runtime NuGet package supplies its MIT license but
        # carries WPF/Windows Forms third-party notices in the accompanying SDK.
        desktops = list((sdk / "sdk").glob("*/Sdks/Microsoft.NET.Sdk.WindowsDesktop"))
        if not desktops:
            raise RuntimeError("Windows Desktop runtime third-party notices are missing from the SDK")
        desktop = max(desktops, key=lambda path: _version_key(path.parents[1].name))
        for name in ("LICENSE.TXT", "THIRD-PARTY-NOTICES.TXT"):
            self.copy(desktop / name, "dotnet/windows-desktop-notices/" + name,
                      f"dotnet-windowsdesktop-sdk:{desktop.parents[1].name}/{name}")

    def finish(self) -> dict:
        manifest = {"format_version": 1, "components": sorted(self.components, key=lambda entry: (entry["kind"], entry["name"])),
                    "files": [self.files[key] for key in sorted(self.files)]}
        path = self.destination / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.destination / "README.txt").write_text(
            "Runtime license and attribution inventory\n\n"
            "These are copies of the actual license and notice files shipped by the installed runtime packages, "
            "plus version-pinned upstream notices missing from binary wheel metadata.\n"
            "manifest.json records component versions, file provenance and SHA-256 hashes.\n"
            "Native-library alternatives and third-party notices are reproduced as supplied by their publishers. "
            "The presence of a commercial-license notice does not assert possession of a commercial license.\n"
            "The application's own GPL license, Springbok notices and corresponding source accompany the package separately.\n",
            encoding="utf-8")
        return manifest


def collect(destination: Path) -> dict:
    """Write a deterministic licenses tree at ``destination`` and return its manifest."""
    collector = _Collector(Path(destination))
    for distribution in RUNTIME_DISTRIBUTIONS:
        collector.distribution(distribution)
    # Only the bootloader is delivered, not the build tool or its pip dependencies.
    collector.distribution("PyInstaller", runtime_note="Bootloader license and distribution exception; build-tool dependencies are not collected")
    collector.python_runtime()
    collector.supplemental()
    collector.dotnet()
    return collector.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    result = collect(arguments.destination)
    print(json.dumps({"destination": str(arguments.destination.resolve()),
                      "components": len(result["components"]), "license_files": len(result["files"])}))
