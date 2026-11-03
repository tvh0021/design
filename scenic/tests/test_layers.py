"""Resumable downloads (layers.download_resumable), no network."""

import hashlib

import pytest

from scenic import layers


def test_full_part_file_is_verified_and_promoted_without_a_request(tmp_path, monkeypatch):
    body = b"x" * 1000
    dest = tmp_path / "tile.tif"
    part = tmp_path / "tile.tif.part"
    part.write_bytes(body)

    def no_request(*a, **k):
        raise AssertionError("no HTTP request expected (a Range from the end would be HTTP 416)")

    monkeypatch.setattr(layers, "_request", no_request)
    info = {"size": len(body), "etag": hashlib.md5(body).hexdigest()}
    e = layers.download_resumable("https://example.invalid/tile.tif", dest, info, quiet=True)
    assert dest.read_bytes() == body and not part.exists()
    assert e["md5_verified"] and e["status"] == "downloaded"


def test_full_part_file_with_bad_md5_is_removed(tmp_path, monkeypatch):
    dest = tmp_path / "tile.tif"
    part = tmp_path / "tile.tif.part"
    part.write_bytes(b"y" * 10)
    monkeypatch.setattr(layers, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(RuntimeError, match="md5"):
        layers.download_resumable("https://example.invalid/t", dest, {"size": 10, "etag": "0" * 32}, quiet=True)
    assert not part.exists() and not dest.exists()
