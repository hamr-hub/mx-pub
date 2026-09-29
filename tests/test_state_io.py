"""Crash-safety and concurrency of the runtime state files.

These files (publish_queue.json, publish_state.json, collections_state.json)
are rewritten after every video. A non-atomic write that is interrupted leaves
truncated JSON, and the next load raises — taking the whole publish queue with
it. `track()` additionally runs from several platform threads at once.
"""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from core.jsonio import atomic_write_json, read_json_safe  # noqa: E402


def test_atomic_write_leaves_no_partial_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"good": 1}')

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"bad": 2})

    assert json.loads(target.read_text()) == {"good": 1}, "old content intact"
    assert not [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")], \
        "no stray temp file left behind"


def test_atomic_write_is_not_observed_half_done(tmp_path):
    """A reader racing the writer only ever sees old or new — never partial."""
    target = tmp_path / "state.json"
    atomic_write_json(target, {"n": 0})
    bad = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                v = json.loads(target.read_text())
                if v.get("n") not in (0, 1000000):
                    bad.append(v)
            except ValueError as e:      # truncated read would land here
                bad.append(repr(e))

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(60):
            atomic_write_json(target, {"n": 0 if i % 2 else 1000000,
                                       "pad": "x" * 20000})
    finally:
        stop.set()
        t.join()
    assert not bad, f"observed a partial write: {bad[:2]}"


def test_read_json_safe_tolerates_garbage():
    assert read_json_safe("/nonexistent/x.json", default={}) == {}
    assert read_json_safe("/etc/hostname", default={"d": 1}) == {"d": 1}


def test_read_json_safe_tolerates_truncated_json(tmp_path):
    f = tmp_path / "broken.json"
    f.write_text('{"platforms": {"douyin": ')
    assert read_json_safe(f, default={}) == {}


def test_track_is_safe_under_concurrent_platform_threads(tmp_path, monkeypatch):
    """core/parallel.py calls track() from a ThreadPoolExecutor."""
    import publisher

    f = tmp_path / "publish_state.json"
    monkeypatch.setattr(publisher, "STATE_FILE", f)

    from publisher import PublishResult

    def worker(platform):
        for i in range(40):
            publisher.track(platform, PublishResult(
                platform, "ok" if i % 2 else "fail", method="cdp"))

    threads = [threading.Thread(target=worker, args=(p,))
               for p in ("douyin", "kuaishou", "weixin")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = json.loads(f.read_text())          # must still be valid JSON
    for p in ("douyin", "kuaishou", "weixin"):
        assert state["platforms"][p]["attempts"] == 40, f"{p} lost updates"
    assert len(state["history"]) == 100        # capped


def test_track_never_raises_on_unwritable_state(monkeypatch):
    """Telemetry must not be able to fail a publish."""
    import publisher

    monkeypatch.setattr(publisher, "STATE_FILE", Path("/proc/nope/state.json"))
    from publisher import PublishResult
    publisher.track("douyin", PublishResult("douyin", "ok", method="cdp"))  # no raise


def test_collections_state_roundtrip_and_tolerates_corruption(tmp_path, monkeypatch):
    import mx_collections

    f = tmp_path / "collections_state.json"
    monkeypatch.setattr(mx_collections, "STATE_FILE", f)

    state = mx_collections._load_state()
    assert state == {"douyin": {}, "kuaishou": {}}

    state["douyin"]["AI合集_20260101"] = {"id": "123"}
    mx_collections._save_state(state)
    assert mx_collections._load_state()["douyin"]["AI合集_20260101"] == {"id": "123"}

    f.write_text("{ not json")
    recovered = mx_collections._load_state()
    assert recovered["douyin"] == {} and recovered["kuaishou"] == {}
