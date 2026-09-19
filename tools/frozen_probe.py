"""Diagnostic-only import/enumeration probe; never starts a tracker or GSPro."""
from __future__ import annotations

import argparse
import faulthandler
import importlib
import json
from pathlib import Path
import sys
import time


START = time.perf_counter()


def report(stage, **values):
    print(json.dumps({"stage": stage, "elapsed_s": round(time.perf_counter() - START, 4), **values}), flush=True)


def timed_import(name):
    report("import.begin", module=name)
    module = importlib.import_module(name)
    report("import.end", module=name, path=str(getattr(module, "__file__", "")))
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enumerate", action="store_true", help="List DirectShow names; does not open cameras")
    parser.add_argument("--stock-order", action="store_true", help="Import camera modules before Qt")
    args = parser.parse_args()
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    faulthandler.enable()
    faulthandler.dump_traceback_later(10, repeat=True)
    report("begin", frozen=bool(getattr(sys, "frozen", False)), executable=sys.executable)
    if args.stock_order:
        for name in ("cv2", "numpy", "cv2_enumerate_cameras"):
            timed_import(name)
    for name in ("PIL.Image", "PIL.ImageDraw", "PySide6.QtCore"):
        timed_import(name)
    qt = sys.modules["PySide6.QtCore"]
    app = qt.QCoreApplication([])
    report("qt.created")

    def camera_work():
        try:
            if not args.stock_order:
                for name in ("cv2", "numpy", "cv2_enumerate_cameras"):
                    timed_import(name)
            putting = timed_import("companion.putting_adapter")
            if args.enumerate:
                report("enumerate.begin")
                devices = putting.camera_devices()
                report("enumerate.end", devices=devices)
            report("complete")
        finally:
            faulthandler.cancel_dump_traceback_later()
            app.quit()

    qt.QTimer.singleShot(0, camera_work)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
