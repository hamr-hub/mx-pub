"""The curation layer must work on Chinese topics.

`content_tokens()` split on [^a-z0-9]+, so every CJK character was discarded
and `signature()` returned "" for all of them. `select_unpublished()` rejects
empty signatures, so with the project's real (100% Chinese) queue the entire
corpus came back as "empty signature" — the curation pipeline could not select
a single video to publish.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from curation import (  # noqa: E402
    Clip,
    content_tokens,
    select_unpublished,
    signature,
)


def _mk(name, topic, day):
    return Clip(path=Path(f"/tmp/{name}.mp4"), day=day, name=name, topic=topic)


def test_chinese_titles_produce_tokens():
    assert content_tokens("深山瀑布") == ["深山", "山瀑", "瀑布"]
    assert content_tokens("一滴水的绽放") == ["一滴", "滴水", "水的", "的绽", "绽放"]


def test_chinese_signatures_are_non_empty_and_distinct():
    assert signature("深山瀑布") == "深山 山瀑 瀑布"
    topics = ["深山瀑布", "北欧冬日极光", "海边悬崖", "热带海岛潜水", "初春樱花树下"]
    sigs = {signature(t) for t in topics}
    assert len(sigs) == len(topics), f"signatures collapsed: {sigs}"
    assert "" not in sigs


def test_single_cjk_character_is_kept():
    assert content_tokens("雪") == ["雪"]


def test_ascii_tokenisation_is_unchanged():
    """Existing ASCII behaviour (and its stop-word rules) must not regress."""
    # stop words (slow/motion/macro) and short words (a/of) stay excluded
    assert content_tokens("a slow motion macro of a droplet at dawn") == [
        "droplet", "dawn"]
    assert signature("Slow motion macro of a droplet blooming") == \
        signature("a droplet blooming in slow motion macro")


def test_mixed_language_keeps_tokens_in_text_order():
    toks = content_tokens("深山 waterfall macro")
    assert toks == ["深山", "waterfall"], toks   # macro is a stop word


def test_chinese_corpus_is_not_rejected_as_empty_signature():
    """The regression that mattered: every Chinese clip was excluded."""
    clips = [_mk("a", "深山瀑布", "20260901"),
             _mk("b", "深山瀑布", "20260920"),      # same topic, newer render
             _mk("c", "北欧冬日极光", "20260902"),
             _mk("d", "海边悬崖", "20260903")]
    clean, rejected = select_unpublished(
        clips, {"douyin": [], "kuaishou": [], "weixin": []})

    assert sorted(c.name for c in clean) == ["b", "c", "d"]
    assert not [r for r in rejected if r[2] == "empty signature"]
    # the older render of the duplicated topic is the only exclusion
    assert [(r[0].name, r[2]) for r in rejected] == [("a", "b.mp4")]


def test_distinct_chinese_topics_are_not_merged():
    topics = ["深山瀑布", "北欧冬日极光", "海边悬崖", "热带海岛潜水"]
    clips = [_mk(f"c{i}", t, f"2026090{i + 1}") for i, t in enumerate(topics)]
    clean, _ = select_unpublished(
        clips, {"douyin": [], "kuaishou": [], "weixin": []})
    assert len(clean) == len(topics), "unrelated topics were merged"


def test_real_queue_topics_all_yield_signatures():
    queue = ROOT / "publish_queue.json"
    if not queue.exists():
        pytest.skip("no queue on this machine")
    videos = json.loads(queue.read_text()).get("videos", [])
    if not videos:
        pytest.skip("empty queue")
    empties = [v.get("title") for v in videos if not signature(v.get("title", ""))]
    assert not empties, f"{len(empties)}/{len(videos)} titles yield no signature"
