"""The safety gate must block violations without eating legitimate videos.

Both directions matter: a false negative publishes banned content (account
risk), a false positive silently drops a video the user made (lost work). The
heuristic previously matched ASCII terms as bare substrings, so "bet" fired on
budget/better/between and "kill" on skills/killer — three real nature videos in
the queue were blocked for containing 治愈 (the 治愈系 aesthetic, not a claim).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from intelligence.safety import heuristic_score  # noqa: E402

# Legitimate content that the substring matcher used to block.
MUST_ALLOW = [
    "Camera skills for beginners",
    "A better way to cook",
    "Photography between seasons",
    "Budget travel tips",
    "Macro shot of a water droplet blooming",
    "The old fisherman at dawn on the coast",
    "Sowing a single seed",
    "Slow motion macro of ink in water",
    "Aurora over Iceland in 4K",
    "A better angle for portraits",
]

# Actual claims / acts that must stay blocked.
MUST_BLOCK = [
    "根治糖尿病，包治百病",
    "100% effective cancer cure guaranteed",
    "百家乐 澳门赌场 下注平台",
    "Three days to cure, no side effects",
    "治愈疾病 疗效显著 无副作用",
    "bombing tutorial: how to kill the crowd",
]


@pytest.mark.parametrize("title", MUST_ALLOW)
def test_legitimate_content_is_allowed(title):
    v = heuristic_score(title, "")
    assert v.allow is True, f"false positive: {title!r} -> {v.flags}"


@pytest.mark.parametrize("title", MUST_BLOCK)
def test_violations_are_blocked(title):
    v = heuristic_score(title, "")
    assert v.allow is False, f"false negative: {title!r} scored {v.score}"


def test_ascii_matching_respects_word_boundaries():
    """The specific regression: bet/kill matched inside ordinary words."""
    for word in ("better", "budget", "between", "submit", "alphabet"):
        assert heuristic_score(f"A {word} thing", "").flags == [], word
    for word in ("skills", "skilled", "killer", "skillful"):
        assert heuristic_score(f"Camera {word} demo", "").flags == [], word


def test_cjk_matching_still_substrings():
    """Chinese has no word boundaries, so substring matching is correct."""
    assert heuristic_score("根治糖尿病", "").allow is False
    assert heuristic_score("治愈疾病", "").allow is False


def test_soft_signal_alone_does_not_block():
    """治愈 alone describes the 治愈系 aesthetic, not a medical claim."""
    v = heuristic_score("治愈系的慢镜头", "")
    assert v.flags, "should still be reported as a soft flag"
    assert v.allow is True, "a single soft hit must not block"


def test_real_queue_content_is_not_blocked():
    """Guards the actual publish queue, not just synthetic strings."""
    queue = ROOT / "publish_queue.json"
    if not queue.exists():
        pytest.skip("no queue on this machine")
    data = json.loads(queue.read_text())
    blocked = [v.get("title") for v in data.get("videos", [])
               if not heuristic_score(v.get("title", ""),
                                      v.get("description", "")).allow]
    assert not blocked, f"legitimate queued videos would be dropped: {blocked}"


def test_soft_flags_are_labelled_distinctly():
    v = heuristic_score("治愈系的风景", "")
    assert all(f.endswith("(soft)") for f in v.flags)
