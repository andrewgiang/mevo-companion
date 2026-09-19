"""Reproduce the source/binary correspondence check using Python 3.11 + PyInstaller.

No vendor code is executed. Compiled source code objects are compared recursively
with code objects extracted from the shipped executable and its embedded PYZ.
"""
import hashlib
import json
import marshal
from pathlib import Path
import sys

if sys.version_info[:2] != (3, 11):
    raise SystemExit("Use Python 3.11: that is the embedded tracker's bytecode version.")

from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parent
archive = CArchiveReader(str(root / "ball_tracking.exe"))
embedded = archive.open_embedded_archive("PYZ-00.pyz")
checks = {}
for name in ("ball_tracking", "ColorModuleExtended"):
    source = root / "source" / f"{name}.py"
    original = marshal.loads(archive.extract(name)) if name == "ball_tracking" else embedded.extract(name)
    compiled = compile(source.read_text(encoding="utf-8"), original.co_filename, "exec")
    checks[name] = {
        "recursive_code_object_equal": original == compiled,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
print(json.dumps(checks, indent=2))
if not all(check["recursive_code_object_equal"] for check in checks.values()):
    raise SystemExit(1)
