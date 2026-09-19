"""Refresh pinned upstream license texts missing from binary wheel metadata.

Run explicitly during dependency updates; normal packaging is entirely offline.
The source URLs and file hashes are checked into supplemental/licenses.json.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent / "license-notices"

SOURCES = {
    "tesseract-5.4.1/LICENSE": "https://raw.githubusercontent.com/tesseract-ocr/tesseract/5.4.1/LICENSE",
    "leptonica-1.84.1/LICENSE": "https://raw.githubusercontent.com/DanBloomberg/leptonica/1.84.1/leptonica-license.txt",
    "libpng-1.6.43/LICENSE": "https://raw.githubusercontent.com/pnggroup/libpng/v1.6.43/LICENSE",
    "zlib-1.3.1/LICENSE": "https://raw.githubusercontent.com/madler/zlib/v1.3.1/LICENSE",
    "libtiff-4.6.0/LICENSE.md": "https://gitlab.com/libtiff/libtiff/-/raw/v4.6.0/LICENSE.md",
    "openjpeg-2.5.2/LICENSE": "https://raw.githubusercontent.com/uclouvain/openjpeg/v2.5.2/LICENSE",
    "libwebp-1.4.0/COPYING": "https://raw.githubusercontent.com/webmproject/libwebp/v1.4.0/COPYING",
    "libwebp-1.4.0/PATENTS": "https://raw.githubusercontent.com/webmproject/libwebp/v1.4.0/PATENTS",
    "zstd-1.5.6/LICENSE": "https://raw.githubusercontent.com/facebook/zstd/v1.5.6/LICENSE",
    "zstd-1.5.6/COPYING": "https://raw.githubusercontent.com/facebook/zstd/v1.5.6/COPYING",
    "xz-5.6.2/COPYING": "https://raw.githubusercontent.com/tukaani-project/xz/v5.6.2/COPYING",
    "xz-5.6.2/COPYING.0BSD": "https://raw.githubusercontent.com/tukaani-project/xz/v5.6.2/COPYING.0BSD",
    "xz-5.6.2/COPYING.LGPLv2.1": "https://raw.githubusercontent.com/tukaani-project/xz/v5.6.2/COPYING.LGPLv2.1",
    "xz-5.6.2/COPYING.GPLv2": "https://raw.githubusercontent.com/tukaani-project/xz/v5.6.2/COPYING.GPLv2",
    "giflib-5.2.2/COPYING": "https://sourceforge.net/p/giflib/code/ci/5.2.2/tree/COPYING?format=raw",
}


def fetch(url):
    with urlopen(Request(url, headers={"User-Agent": "MevoCompanion-license-inventory"}), timeout=30) as response:
        data = response.read()
    if data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("Received HTML instead of the requested notice")
    return data


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Preserve QtBase's actual third-party attribution files and notices as well
    # as complete LGPL/GPL terms. These supplement wheels missing those texts.
    tree = json.loads(fetch("https://api.github.com/repos/qt/qtbase/git/trees/v6.7.3?recursive=1"))
    for entry in tree["tree"]:
        path = entry["path"]
        basename = Path(path).name.upper()
        if entry["type"] == "blob" and (path.startswith("LICENSES/") or
            path.startswith("src/3rdparty/") and (basename.startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE")) or basename == "QT_ATTRIBUTION.JSON")):
            SOURCES["qtbase-6.7.3/" + path] = "https://raw.githubusercontent.com/qt/qtbase/v6.7.3/" + path
    results, errors = [], []

    def download(item):
        path, url = item
        data = fetch(url)
        destination = ROOT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {"file": path, "url": url, "sha256": hashlib.sha256(data).hexdigest()}

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [(item, executor.submit(download, item)) for item in sorted(SOURCES.items())]
        for item, future in futures:
            try:
                results.append(future.result())
            except Exception as error:
                errors.append({"file": item[0], "url": item[1], "error": str(error)})
    # IJG distributes its exact 9f source release, including the license in README.
    url = "https://www.ijg.org/files/jpegsrc.v9f.tar.gz"
    try:
        source = fetch(url)
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
            data = archive.extractfile("jpeg-9f/README").read()
        path = "jpeg-9f/README"
        destination = ROOT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        results.append({"file": path, "url": url, "member": "jpeg-9f/README",
                        "archive_sha256": hashlib.sha256(source).hexdigest(), "sha256": hashlib.sha256(data).hexdigest()})
    except Exception as error:
        errors.append({"file": "jpeg-9f/README", "url": url, "error": str(error)})
    (ROOT / "sources.json").write_text(json.dumps({"files": sorted(results, key=lambda row: row["file"]), "missing": errors}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"collected": len(results), "missing": errors}, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
