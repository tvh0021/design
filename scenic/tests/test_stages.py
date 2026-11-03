"""Stage runner freshness: code, config sections and input contents all go
into a stage's fingerprint, and a changed output reruns what reads it."""

import os
from pathlib import Path

import pytest

from scenic import score_stages as ss
from scenic.score_stages import (FileHashes, Paths, Stage, TrackedConfig, code_hash, config_hash, is_fresh,
                                 stage_fingerprint, write_stamp)


def body_a(paths):
    return {}


def body_b(paths):
    return {}


def test_code_hash_follows_module_source(tmp_path):
    (tmp_path / "mod.py").write_text("X = 1\n")
    h1 = code_hash(body_a, ("mod",), tmp_path)
    assert code_hash(body_a, ("mod",), tmp_path) == h1
    (tmp_path / "mod.py").write_text("X = 2\n")
    assert code_hash(body_a, ("mod",), tmp_path) != h1
    assert code_hash(body_b, ("mod",), tmp_path) != code_hash(body_a, ("mod",), tmp_path)


def test_config_hash_only_sees_declared_sections():
    cfg = {"combine": {"w": 0.4}, "water": {"eye_m": 1.2}}
    h = config_hash(cfg, ("combine",))
    assert config_hash({**cfg, "water": {"eye_m": 2.0}}, ("combine",)) == h
    assert config_hash({**cfg, "combine": {"w": 0.35}}, ("combine",)) != h


def test_tracked_config_records_sections():
    TrackedConfig.accessed = set()
    c = TrackedConfig({"a": {"x": 1}, "b": 2})
    assert c["a"]["x"] == 1 and c.get("c") is None
    assert TrackedConfig.accessed == {"a", "c"}


def _run(stage, code_h, cfg_h, hashes, write):
    """Mimic the runner for one stage: rerun (write outputs) when stale."""
    fp = stage_fingerprint(code_h, cfg_h, stage.inputs, hashes)
    if is_fresh(stage, fp, hashes):
        return False
    write()
    write_stamp(stage, fp, hashes)
    return True


def _touch(p: Path, text: str):
    p.write_text(text)
    st = p.stat()  # make sure the mtime moves even on coarse clocks
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_code_change_reruns_the_stage_and_what_reads_its_output(tmp_path):
    src, a_out, b_out = tmp_path / "in.txt", tmp_path / "a.out", tmp_path / "b.out"
    src.write_text("input")
    A = Stage("a", body_a, [src], [a_out])
    B = Stage("b", body_b, [a_out], [b_out])
    hashes = FileHashes(tmp_path / "cache.json")
    a_text = {"v": "A1"}
    runA = lambda code: _run(A, code, "cfg", hashes, lambda: _touch(a_out, a_text["v"]))  # noqa: E731
    runB = lambda cfg="cfg": _run(B, "codeB", cfg, hashes, lambda: _touch(b_out, "B"))  # noqa: E731
    assert runA("codeA") and runB()
    assert not runA("codeA") and not runB()            # nothing changed: both fresh
    # A's formula changes but its output is byte-identical: B stays fresh.
    assert runA("codeA2") and not runB()
    # A's formula changes its output: B reruns.
    a_text["v"] = "A2"
    assert runA("codeA3") and runB()
    # B's own config section changes: only B reruns.
    assert not runA("codeA3") and runB("cfg2")
    # Someone overwrites B's output: B reruns.
    _touch(b_out, "edited")
    assert runB("cfg2")
    # File hashes are cached by size and mtime and survive a reload.
    hashes.save()
    assert FileHashes(tmp_path / "cache.json")(a_out) == hashes(a_out)


def test_every_stage_declares_existing_modules(tmp_path):
    paths = Paths(graph=str(tmp_path), raw=str(tmp_path), rasters=str(tmp_path), out=str(tmp_path),
                  config=str(ss.PKG_DIR.parents[1] / "config" / "score.toml"))
    names = [s.name for s in ss.stages(paths)]
    assert len(names) == len(set(names))
    for st in ss.stages(paths):
        assert all((ss.PKG_DIR / f"{m}.py").exists() for m in st.modules), st.name
        assert len(code_hash(st.fn, st.modules)) == 64


@pytest.mark.parametrize("missing", [True, False])
def test_file_hashes_missing_file(tmp_path, missing):
    p = tmp_path / "x"
    if not missing:
        p.write_text("1")
    assert (FileHashes()(p) == "missing") == missing
