"""AI content generation: what actually gets published.

Two defects: a blank `title` from the LLM was accepted (publishing a post with
no title at all), and non-string / empty hashtags reached `.lstrip` unfiltered.
Separately, resolving a source video walked the whole 60k-entry media tree once
per video — and twice, because both search roots resolve to the same directory.
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from ai import content as C  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_media_roots(tmp_path, monkeypatch):
    """Point the media search at a tiny temp tree and drop caches."""
    media = tmp_path / "images"
    (media / "sub").mkdir(parents=True)
    (media / "sub" / "clip01.mp4").write_bytes(b"x")
    (media / "top.mov").write_bytes(b"x")
    monkeypatch.setattr(C, "_VIDEO_SEARCH_ROOTS", [media])
    C._reset_video_lookup_cache()
    yield
    C._reset_video_lookup_cache()


def _stub_response(monkeypatch, payload):
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setattr(C, "_parse_json_response", lambda text: payload)


def test_blank_title_falls_back_instead_of_publishing_an_empty_title(monkeypatch):
    _stub_response(monkeypatch, {"title": "", "description": "d",
                                 "hashtags": ["a"]})
    result = C.generate_content("/tmp/whatever.mp4", "douyin")
    assert result["title"].strip(), "an empty title would waste a publish"
    assert result["title"] != ""


def test_whitespace_only_title_falls_back(monkeypatch):
    _stub_response(monkeypatch, {"title": "   \n  ", "description": "d"})
    assert C.generate_content("/tmp/whatever.mp4", "douyin")["title"].strip()


def test_title_is_trimmed_and_truncated(monkeypatch):
    _stub_response(monkeypatch, {"title": "  一二三四五6789  ",
                                 "description": "d", "hashtags": []})
    out = C.generate_content("/tmp/whatever.mp4", "douyin")
    assert out["title"] == "一二三四五6789"


def test_hashtags_are_coerced_and_cleaned(monkeypatch):
    _stub_response(monkeypatch, {"title": "标题", "description": "d",
                                 "hashtags": [1, 2, None, "#ok", "  ", ""]})
    out = C.generate_content("/tmp/whatever.mp4", "douyin")
    assert out["hashtags"] == ["1", "2", "ok"]


def test_single_hashtag_string_is_tolerated(monkeypatch):
    _stub_response(monkeypatch, {"title": "标题", "description": "d",
                                 "hashtags": "#solo"})
    out = C.generate_content("/tmp/whatever.mp4", "douyin")
    assert out["hashtags"] == ["solo"]


def test_hashtags_are_capped(monkeypatch):
    _stub_response(monkeypatch, {"title": "标题", "description": "d",
                                 "hashtags": [f"t{i}" for i in range(20)]})
    assert len(C.generate_content("/tmp/whatever.mp4", "douyin")["hashtags"]) == 5


def test_source_video_is_found_in_the_index():
    assert C._find_source_video("clip01.mp4").endswith("sub/clip01.mp4")
    assert C._find_source_video("top.mov").endswith("top.mov")


def test_missing_source_video_returns_none():
    assert C._find_source_video("absent.mp4") is None


def test_one_index_serves_every_lookup(monkeypatch):
    """The whole tree must be walked once, not once per filename."""
    calls = {"n": 0}
    real_walk = C.os.walk

    def counting_walk(*a, **k):
        calls["n"] += 1
        return real_walk(*a, **k)

    monkeypatch.setattr(C.os, "walk", counting_walk)
    C._reset_video_lookup_cache()
    for name in ("a.mp4", "b.mp4", "c.mp4", "clip01.mp4", "absent.mp4"):
        C._find_source_video(name)
    assert calls["n"] <= 1, f"tree walked {calls['n']} times for 5 lookups"


def test_duplicate_search_roots_are_walked_once(tmp_path, monkeypatch):
    """Both configured roots usually resolve to the same directory."""
    media = tmp_path / "dup_roots"
    media.mkdir()
    (media / "x.mp4").write_bytes(b"x")
    monkeypatch.setattr(C, "_VIDEO_SEARCH_ROOTS", [media, media, media])
    C._reset_video_lookup_cache()
    calls = {"n": 0}
    real_walk = C.os.walk

    def counting_walk(*a, **k):
        calls["n"] += 1
        return real_walk(*a, **k)

    monkeypatch.setattr(C.os, "walk", counting_walk)
    C._find_source_video("x.mp4")
    assert calls["n"] == 1


def test_repeated_lookup_is_served_from_cache():
    t0 = time.time()
    C._find_source_video("clip01.mp4")
    first = time.time() - t0
    t0 = time.time()
    C._find_source_video("clip01.mp4")
    assert time.time() - t0 <= first


# ---------------------------------------------------------------------------
# Ambiguous basenames: the media tree holds several video_1.mp4 files with
# different prompts.csv seeds. Falling through to a first-match basename search
# silently picked the wrong one and generated content from the wrong prompt.
# ---------------------------------------------------------------------------

def test_existing_full_path_is_used_directly(tmp_path, monkeypatch):
    media = tmp_path / "fullpath"
    (media / "20260825" / "minimax").mkdir(parents=True)
    wanted = media / "20260825" / "minimax" / "video_1.mp4"
    wanted.write_bytes(b"x")
    # a decoy with the same basename elsewhere in the tree
    (media / "20260620").mkdir()
    (media / "20260620" / "video_1.mp4").write_bytes(b"x")
    monkeypatch.setattr(C, "_VIDEO_SEARCH_ROOTS", [media])
    C._reset_video_lookup_cache()
    assert C._resolve_video_path(str(wanted)) == str(wanted)


def test_ambiguous_basename_prefers_the_matching_directory(tmp_path, monkeypatch):
    media = tmp_path / "ambiguous"
    (media / "20260825").mkdir(parents=True)
    (media / "20260620").mkdir()
    (media / "20260620" / "video_1.mp4").write_bytes(b"x")
    monkeypatch.setattr(C, "_VIDEO_SEARCH_ROOTS", [media])
    C._reset_video_lookup_cache()
    asked = str(media / "20260825" / "video_1.mp4")   # does not exist
    assert C._resolve_video_path(asked) == \
        str(media / "20260620" / "video_1.mp4"), "only candidate"


def test_no_tree_walk_when_the_path_is_usable(tmp_path, monkeypatch):
    """The caller already has the path; walking 60k entries is wasted work."""
    media = tmp_path / "nowalk"
    media.mkdir()
    target = media / "clip.mp4"
    target.write_bytes(b"x")
    monkeypatch.setattr(C, "_VIDEO_SEARCH_ROOTS", [media])
    C._reset_video_lookup_cache()
    calls = {"n": 0}
    real_walk = C.os.walk

    def counting_walk(*a, **k):
        calls["n"] += 1
        return real_walk(*a, **k)

    monkeypatch.setattr(C.os, "walk", counting_walk)
    assert C._resolve_video_path(str(target)) == str(target)
    assert calls["n"] == 0, "must not walk when the path already resolves"
