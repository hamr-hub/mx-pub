"""Unit tests for content curation primitives (scripts/curation.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from curation import (  # noqa: E402
    Clip,
    content_tokens,
    is_live_topic,
    normalize,
    select_unpublished,
    signature,
)


def test_normalize_strips_punctuation_and_cjk_boundaries() -> None:
    assert normalize("Slow-Motion Macro! 一滴水") == "slow motion macro 一滴水"


def test_content_tokens_drop_stop_words_and_short_words() -> None:
    toks = content_tokens("a slow motion macro of a droplet at dawn")
    assert "slow" not in toks        # stop word
    assert "of" not in toks          # stop word + short
    assert "macro" not in toks       # photographic-direction stop word
    assert "droplet" in toks
    assert "dawn" in toks


def test_signature_keeps_subject_order() -> None:
    a = signature("tiger chasing a fisherman along the shore")
    b = signature("fisherman chasing a tiger along the shore")
    assert a != b
    assert a == "tiger chasing fisherman along shore"


def test_live_topic_matches_truncated_platform_head() -> None:
    # Old runs posted only the first ~33 chars of the prompt as title.
    topic = ("lone astronaut walking away across red Mars dust plain at "
             "sunset, long shadow, anamorphic, Villeneuve palette")
    truncated_head = "lone astronaut walking away across red Mars dust"
    assert is_live_topic(topic, [truncated_head]) == truncated_head


def test_live_topic_order_matters() -> None:
    topic = "tiger chasing a fisherman along the shore at dawn"
    assert is_live_topic(topic, ["fisherman chasing tiger along the shore"]) is None


def test_live_topic_unrelated_content_is_clean() -> None:
    topic = "macro droplet impacting black lake surface at dawn, ripples"
    assert is_live_topic(topic, ["北欧冬日极光", "火星落日独行"]) is None


def test_live_topic_empty_live_set_never_matches() -> None:
    assert is_live_topic("macro droplet on lake", []) is None


def _clip(day: str, name: str, topic: str) -> Clip:
    return Clip(path=Path(f"/x/{day}/{name}"), day=day, name=name, topic=topic)


def test_select_keeps_newest_render_per_signature() -> None:
    topic = ("lone astronaut walking away across red Mars dust plain at "
             "sunset, long shadow")
    older = _clip("20260912", "video_1.mp4", topic)
    newer = _clip("20260914", "video_2.mp4", topic)
    clean, rejected = select_unpublished([older, newer], {})
    assert [c.name for c in clean] == ["video_2.mp4"]
    assert ("video_1.mp4", "duplicate-topic", "video_2.mp4") in [
        (Path(r[0].path).name, r[1], r[2]) for r in rejected]


def test_select_excludes_live_and_unknown_topics() -> None:
    live = {"douyin": ["campfire in mountain wilderness at night embers"],
            "kuaishou": [], "weixin": []}
    posted = _clip("20260824", "v5.mp4",
                   "campfire in mountain wilderness at night, embers rising")
    unknown = _clip("20260801", "v9.mp4", "")
    fresh = _clip("20260913", "v1.mp4",
                  "macro droplet impacting black lake surface at dawn")
    clean, rejected = select_unpublished([posted, unknown, fresh], live)
    assert [c.name for c in clean] == ["v1.mp4"]
    reasons = {(Path(r[0].path).name, r[1]) for r in rejected}
    assert ("v5.mp4", "douyin") in reasons
    assert ("v9.mp4", "metadata") in reasons


def test_select_requires_clean_on_every_platform() -> None:
    # Live only on weixin still disqualifies.
    live = {"douyin": [], "kuaishou": [],
            "weixin": ["Kyoto temple courtyard maple autumn leaves"]}
    clip = _clip("20260912", "v2.mp4",
                 "Kyoto temple courtyard maple leaves, autumn, poetic")
    clean, _ = select_unpublished([clip], live)
    assert clean == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
