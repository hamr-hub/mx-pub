"""End-to-end control-flow test for the safety gate inside run_platform().

This drives the REAL publish loop with a fake Playwright session and a fake
platform module — no Chrome, no network. It is the only test that proves the
queue-level semantics of a safety veto, which is the difference between
"skip one video" and "silently cancel the rest of the batch".
"""
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import batch_publish  # noqa: E402
from publisher import PublishResult  # noqa: E402

N = 4
FLAGGED = 1


def _make_queue(n=N):
    return {
        "total": n,
        "videos": [
            {"name": f"v{i}.mp4", "title": f"video-{i}",
             "path": f"/tmp/v{i}.mp4", "description": "d",
             "hashtags": ["t"], "published": {}}
            for i in range(n)
        ],
    }


def _run_loop(monkeypatch, *, safety_env=None, save_queue=None):
    """Run the real run_platform loop; return (attempted titles, summary)."""
    attempted: list[str] = []

    class FakeMod:
        @staticmethod
        def publish_on_page(page, title, description, video, topics,
                            location, collection):
            attempted.append(title)
            return PublishResult("douyin", "ok", method="fake")

        @staticmethod
        def _setup_page(page):
            pass

    class FakePage:
        url = "about:blank"

    class FakeCtx:
        pages = [FakePage()]

        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeCtx()]

    driver = types.SimpleNamespace(stop=lambda: None)
    driver.chromium = types.SimpleNamespace(
        connect_over_cdp=lambda url, timeout=0: FakeBrowser())

    fake_pw = types.SimpleNamespace(start=lambda: driver)
    monkeypatch.setattr(batch_publish, "_get_module", lambda p: FakeMod)
    monkeypatch.setattr(batch_publish, "is_publishable_duration",
                        lambda p: (True, 10.0))
    monkeypatch.setattr(batch_publish, "save_queue",
                        save_queue or (lambda q: None))
    monkeypatch.setattr(batch_publish, "reconcile_douyin_collections",
                        lambda *a, **k: None)
    monkeypatch.setattr(batch_publish, "attach_kuaishou_collections",
                        lambda *a, **k: None)
    monkeypatch.setenv("MX_MAX_RECOVERY", "0")
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: fake_pw, raising=False)
    # raw_cdp.tab probe: pretend a tab already exists
    monkeypatch.setattr("core.raw_cdp.list_tabs", lambda: [{"type": "page"}],
                        raising=False)

    monkeypatch.delenv("MX_SAFETY_ABORT_BATCH", raising=False)
    if safety_env is not None:
        monkeypatch.setenv("MX_SAFETY_ABORT_BATCH", safety_env)

    # safety gate: veto exactly one video through the real hook pipeline
    from core.hooks import Hooks

    def _veto(video=None, platform=None, **kw):
        if video.get("title") == f"video-{FLAGGED}":
            video["_safety_verdict"] = {"allow": False, "reason": "gambling"}

    Hooks.register("video:before_publish", _veto)
    monkeypatch.setattr(batch_publish, "_HAS_NEXT_GEN", True)
    try:
        summary = batch_publish.run_platform("douyin", _make_queue(),
                                             "http://127.0.0.1:9222")
    finally:
        Hooks._hooks.pop("video:before_publish", None)
    return attempted, summary


def test_safety_veto_skips_only_the_flagged_video(monkeypatch):
    """The regression: a veto used to `break` the whole while-loop, which
    cancelled every remaining video and never recorded the block."""
    attempted, summary = _run_loop(monkeypatch)

    assert attempted == ["video-0", "video-2", "video-3"], (
        "the flagged video must be skipped, the rest of the queue must run")
    assert summary["blocked"] == 1
    assert summary["ok"] == N - 1


def test_safety_veto_is_persisted_to_the_queue(monkeypatch):
    """A block that is not written back is resurrected on the next run."""
    saved: list[dict] = []
    _run_loop(monkeypatch, save_queue=lambda q: saved.append(json_copy(q)))
    assert saved, "the blocked row must be written back to the queue"
    row = next(v for v in saved[-1]["videos"] if v["title"] == "video-1")
    assert row["published"]["douyin"]["status"] == "blocked"
    # ...and must not be re-attempted next run
    assert batch_publish.needs_publish(row, "douyin") is False


def json_copy(obj):
    import copy
    return copy.deepcopy(obj)


def test_safety_abort_batch_stops_the_platform(monkeypatch):
    """Opt-in escape hatch for operators who want a violation to halt the day."""
    attempted, summary = _run_loop(monkeypatch, safety_env="1")

    assert attempted == ["video-0"], "batch abort must stop after the violation"
    assert "safety_abort_batch" in summary["errors"]


def test_abort_batch_env_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MX_SAFETY_ABORT_BATCH", raising=False)
    assert batch_publish._abort_batch_on_safety() is False
    monkeypatch.setenv("MX_SAFETY_ABORT_BATCH", "1")
    assert batch_publish._abort_batch_on_safety() is True
    monkeypatch.setenv("MX_SAFETY_ABORT_BATCH", "off")
    assert batch_publish._abort_batch_on_safety() is False
