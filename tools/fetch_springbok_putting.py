"""Fetch the unchanged, checksum-pinned Springbok putting executable for a build."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from urllib.request import Request, urlopen
import zipfile


VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "springbok-putting"


def verify(stream, expected: str, label: str) -> None:
    stream.seek(0)
    actual = hashlib.file_digest(stream, "sha256").hexdigest()
    stream.seek(0)
    if actual != expected:
        raise ValueError(f"{label} SHA-256 mismatch; no executable was installed.")


def ensure_tracker(vendor: Path = VENDOR, archive_path: Path | None = None) -> Path:
    provenance = json.loads((vendor / "provenance.json").read_text(encoding="utf-8"))
    destination = vendor / "ball_tracking.exe"
    expected = provenance["files"]["ball_tracking.exe"]
    if destination.exists():
        with destination.open("rb") as existing:
            verify(existing, expected, "Existing ball_tracking.exe")
        return destination

    # The complete release is verified before any member is used. Read only the
    # tracker member; never extract paths supplied by the archive.
    with tempfile.TemporaryFile() as archive:
        if archive_path is not None:
            with archive_path.open("rb") as source:
                shutil.copyfileobj(source, archive)
        else:
            request = Request(provenance["release_url"], headers={"User-Agent": "MevoCompanion-build"})
            with urlopen(request, timeout=60) as response:
                shutil.copyfileobj(response, archive)
        verify(archive, provenance["release_sha256"], "Springbok release archive")
        with zipfile.ZipFile(archive) as release:
            members = [member for member in release.infolist()
                       if not member.is_dir() and PurePosixPath(member.filename).name == "ball_tracking.exe"]
            if len(members) != 1:
                raise ValueError("Expected exactly one ball_tracking.exe in the pinned release.")
            staged = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".putting-", suffix=".tmp", dir=vendor,
                                                 mode="w+b", delete=False) as output:
                    staged = Path(output.name)
                    with release.open(members[0]) as source:
                        shutil.copyfileobj(source, output)
                    output.flush()
                    verify(output, expected, "Downloaded ball_tracking.exe")
                os.replace(staged, destination)
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use a locally downloaded pinned release ZIP instead of downloading it.")
    args = parser.parse_args()
    try:
        destination = ensure_tracker(archive_path=args.archive)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Putting prerequisite failed: {error}\n")
    print(f"Verified {destination}")


if __name__ == "__main__":
    main()
