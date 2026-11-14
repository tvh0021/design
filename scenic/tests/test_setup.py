import io
import tarfile
import zipfile
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import scenic
from scenic import cli, setup_data
from scenic.web import server as web


def _write_toy_runtime(root: Path) -> None:
    gdir, sdir = root / "graph", root / "score"
    gdir.mkdir(parents=True, exist_ok=True)
    sdir.mkdir(parents=True, exist_ok=True)
    np.savez(gdir / "nodes.npz", lon=np.array([-77.0, -76.9]), lat=np.array([43.0, 43.0]), lcc=np.array([True, True]))
    edges = pa.table({
        "u": pa.array([0], type=pa.int64()),
        "v": pa.array([1], type=pa.int64()),
        "way_id": pa.array([101], type=pa.int64()),
        "oneway": pa.array([False]),
        "time_s": pa.array([60.0]),
        "length_m": pa.array([1000.0]),
        "lon": pa.array([[-77.0, -76.9]], type=pa.list_(pa.float64())),
        "lat": pa.array([[43.0, 43.0]], type=pa.list_(pa.float64())),
    })
    scores = pa.table({
        "u": pa.array([0], type=pa.int64()),
        "v": pa.array([1], type=pa.int64()),
        "way_id": pa.array([101], type=pa.int64()),
        "S": pa.array([0.75]),
    })
    pq.write_table(edges, gdir / "edges.parquet")
    pq.write_table(scores, sdir / "scores.parquet")


def test_data_and_config_dirs_support_standalone_install(tmp_path, monkeypatch):
    monkeypatch.delenv("SCENIC_DATA", raising=False)
    assert (scenic.config_dir() / "score.toml").is_file()

    fake_pkg = tmp_path / "site-packages" / "scenic"
    (fake_pkg / "_config").mkdir(parents=True)
    (fake_pkg / "_config" / "score.toml").write_text("[route]\n")
    monkeypatch.setattr(scenic, "__file__", str(fake_pkg / "__init__.py"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert scenic.data_dir() == tmp_path / "xdg" / "scenic"
    assert scenic.config_dir() == fake_pkg / "_config"

    monkeypatch.setenv("SCENIC_DATA", str(tmp_path / "custom"))
    assert scenic.data_dir() == tmp_path / "custom"


def test_missing_data_fails_fast_with_setup_instructions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SCENIC_DATA", str(tmp_path / "empty"))
    assert cli.main(["serve"]) == 1
    err = capsys.readouterr().err
    assert "scenic setup" in err and "nodes.npz" in err
    assert web.serve(port=0, graph_dir=tmp_path / "empty" / "graph", score_dir=tmp_path / "empty" / "score") == 1


def test_serve_open_browser_launches_loopback_url(tmp_path, monkeypatch):
    _write_toy_runtime(tmp_path)
    opened = []

    class DummyServer:
        server_address = ("127.0.0.1", 8799)

        def serve_forever(self):
            return None

        def server_close(self):
            return None

    class ImmediateTimer:
        def __init__(self, interval, fn, args=(), kwargs=None):
            self._fn = fn
            self._args = args
            self._kwargs = kwargs or {}
            self.daemon = False

        def start(self):
            self._fn(*self._args, **self._kwargs)

    monkeypatch.setattr(web, "make_server", lambda app, port=0, verbose=False: DummyServer())
    monkeypatch.setattr(web.threading, "Timer", ImmediateTimer)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert web.serve(port=8799, graph_dir=tmp_path / "graph", score_dir=tmp_path / "score", open_browser=True) == 0
    assert opened == ["http://127.0.0.1:8799/"]


def test_setup_check_and_bundle_install(tmp_path, monkeypatch):
    src = tmp_path / "src_data"
    _write_toy_runtime(src)
    bundle = tmp_path / "runtime.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        for rel in ("graph/nodes.npz", "graph/edges.parquet", "score/scores.parquet"):
            tf.add(src / rel, arcname=f"scenic-ny/{rel}")

    dest = tmp_path / "installed"
    monkeypatch.setenv("SCENIC_DATA", str(dest))
    assert cli.main(["setup", "--check"]) == 1
    assert cli.main(["setup", "--bundle", str(bundle)]) == 0
    assert cli.main(["setup", "--check"]) == 0


def test_bundle_rejects_traversal_and_symlinks(tmp_path):
    bad_tar = tmp_path / "traversal.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as tf:
        info = tarfile.TarInfo("../escape.txt")
        data = b"nope"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="Unsafe archive path"):
        setup_data.install_bundle(bad_tar, data_root=tmp_path / "out1")

    bad_zip = tmp_path / "symlink.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zi = zipfile.ZipInfo("graph/nodes.npz")
        zi.create_system = 3
        zi.external_attr = (0o120777 << 16)
        zf.writestr(zi, "/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        setup_data.install_bundle(bad_zip, data_root=tmp_path / "out2")
