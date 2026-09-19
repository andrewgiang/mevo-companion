import hashlib
import json
import zipfile

import pytest

from tools import fetch_springbok_putting as fetch


def fixture_release(tmp_path, payload=b"pinned tracker"):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("release/ball_tracking.exe", payload)
        output.writestr("../unrelated.txt", b"must not be extracted")
    provenance = {
        "release_url": "https://example.invalid/pinned-release.zip",
        "release_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "files": {"ball_tracking.exe": hashlib.sha256(payload).hexdigest()},
    }
    (vendor / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    return vendor, archive, provenance


def test_verified_archive_installs_only_tracker_and_existing_copy_needs_no_download(tmp_path, monkeypatch):
    vendor, archive, _ = fixture_release(tmp_path)
    destination = fetch.ensure_tracker(vendor, archive)
    assert destination.read_bytes() == b"pinned tracker"
    assert sorted(path.name for path in vendor.iterdir()) == ["ball_tracking.exe", "provenance.json"]
    assert not (tmp_path / "unrelated.txt").exists()
    monkeypatch.setattr(fetch, "urlopen", lambda *args, **kwargs: pytest.fail("Existing tracker must not download"))
    assert fetch.ensure_tracker(vendor) == destination


def test_bad_release_checksum_does_not_install(tmp_path):
    vendor, archive, _ = fixture_release(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"modified")
    with pytest.raises(ValueError, match="release archive SHA-256 mismatch"):
        fetch.ensure_tracker(vendor, archive)
    assert list(vendor.iterdir()) == [vendor / "provenance.json"]


def test_bad_tracker_checksum_cleans_staging_file(tmp_path):
    vendor, archive, provenance = fixture_release(tmp_path)
    provenance["files"]["ball_tracking.exe"] = hashlib.sha256(b"other tracker").hexdigest()
    (vendor / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    with pytest.raises(ValueError, match="Downloaded ball_tracking.exe SHA-256 mismatch"):
        fetch.ensure_tracker(vendor, archive)
    assert list(vendor.iterdir()) == [vendor / "provenance.json"]


def test_bad_existing_tracker_is_preserved_and_never_downloaded(tmp_path, monkeypatch):
    vendor, _, _ = fixture_release(tmp_path)
    destination = vendor / "ball_tracking.exe"
    destination.write_bytes(b"unverified existing file")
    monkeypatch.setattr(fetch, "urlopen", lambda *args, **kwargs: pytest.fail("Existing tracker must not download"))
    with pytest.raises(ValueError, match="Existing ball_tracking.exe SHA-256 mismatch"):
        fetch.ensure_tracker(vendor)
    assert destination.read_bytes() == b"unverified existing file"
