"""Tests for AI content generation providers.

Verifies the full code path runs end-to-end:
- Mock provider: deterministic, no network
- Heuristic fallback: reads prompts.csv
- Claude/OpenAI/Ollama paths: gracefully fall back when API key missing

Run: python -m pytest tests/  (or pytest directly)
Or:  python tests/test_ai_providers.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

from ai.content import (
    generate_content,
    _call_mock,
    _heuristic_content,
    _read_prompt_for_video,
    _parse_json_response,
    TITLE_LIMITS,
    PLATFORM_STYLE,
)


TEST_VIDEO = "/Users/hyx/ssd/codespace/personal/images/20260825/minimax/video_1.mp4"
TEST_VIDEO_CHERRY = "/Users/hyx/ssd/codespace/personal/images/20260825/minimax/video_2.mp4"


def assert_eq(actual, expected, msg=""):
    assert actual == expected, f"{msg}: expected {expected!r}, got {actual!r}"


def assert_true(cond, msg=""):
    assert cond, msg


def test_constants():
    """Title limits and styles are defined per platform."""
    for p in ["xhs", "douyin", "kuaishou", "weixin"]:
        assert p in TITLE_LIMITS, f"missing title limit for {p}"
        assert p in PLATFORM_STYLE, f"missing style for {p}"
        assert TITLE_LIMITS[p] > 0


def test_parse_json_clean():
    """Parse plain JSON."""
    result = _parse_json_response('{"title": "hello", "tags": ["a", "b"]}')
    assert_eq(result["title"], "hello")
    assert_eq(result["tags"], ["a", "b"])


def test_parse_json_with_markdown():
    """Parse JSON wrapped in markdown code fence."""
    text = '```json\n{"title": "hello"}\n```'
    result = _parse_json_response(text)
    assert_eq(result["title"], "hello")


def test_parse_json_embedded():
    """Parse JSON embedded in surrounding text."""
    text = 'Here is the result:\n{"title": "hi"}\nHope you like it.'
    result = _parse_json_response(text)
    assert_eq(result["title"], "hi")


def test_parse_json_invalid():
    """Returns None for invalid JSON."""
    assert_eq(_parse_json_response("not json"), None)
    assert_eq(_parse_json_response(""), None)


def test_mock_returns_valid_json():
    """Mock provider returns parseable JSON."""
    prompt = "测试 prompt"
    result = _call_mock(prompt)
    parsed = _parse_json_response(result)
    assert parsed is not None
    assert "title" in parsed
    assert "description" in parsed
    assert "hashtags" in parsed


def test_heuristic_no_api_key():
    """Heuristic fallback produces valid content without API keys."""
    if not Path(TEST_VIDEO).exists():
        print("  [skip] test video not found:", TEST_VIDEO)
        return
    for platform in ["xhs", "douyin", "kuaishou", "weixin"]:
        content = generate_content(TEST_VIDEO, platform=platform)
        assert "title" in content
        assert "description" in content
        assert "hashtags" in content
        # Title must respect platform limit
        assert len(content["title"]) <= TITLE_LIMITS[platform], \
            f"title too long for {platform}: {len(content['title'])} > {TITLE_LIMITS[platform]}"


def test_heuristic_uses_prompts_csv():
    """Heuristic reads topic_seed from prompts.csv."""
    if not Path(TEST_VIDEO).exists():
        print("  [skip] test video not found:", TEST_VIDEO)
        return
    seed = _read_prompt_for_video(TEST_VIDEO)
    assert seed, "prompts.csv lookup failed"
    assert "lightning" in seed.lower() or "storm" in seed.lower(), \
        f"seed doesn't mention lightning/storm: {seed[:100]}"


def test_mock_provider_full_path():
    """Mock provider goes through the full AI code path (no heuristic fallback)."""
    if not Path(TEST_VIDEO).exists():
        print("  [skip] test video not found:", TEST_VIDEO)
        return
    os.environ["MOCK_AI"] = "1"
    try:
        content = generate_content(TEST_VIDEO, platform="xhs")
        assert "lightning" in content["title"].lower() or "storm" in content["title"].lower(), \
            f"mock should produce real content, got: {content}"
    finally:
        del os.environ["MOCK_AI"]


def test_platform_specific_content():
    """Different platforms produce different style for same video."""
    if not Path(TEST_VIDEO_CHERRY).exists():
        print("  [skip] test video not found:", TEST_VIDEO_CHERRY)
        return
    xhs = generate_content(TEST_VIDEO_CHERRY, platform="xhs")
    dy = generate_content(TEST_VIDEO_CHERRY, platform="douyin")
    # Should differ in description (style templates differ)
    assert xhs["description"] != dy["description"], \
        "xhs and douyin descriptions should differ"


def main():
    """Run all tests in order."""
    tests = [
        ("constants", test_constants),
        ("parse_json_clean", test_parse_json_clean),
        ("parse_json_markdown", test_parse_json_with_markdown),
        ("parse_json_embedded", test_parse_json_embedded),
        ("parse_json_invalid", test_parse_json_invalid),
        ("mock_returns_json", test_mock_returns_valid_json),
        ("heuristic_no_key", test_heuristic_no_api_key),
        ("heuristic_uses_csv", test_heuristic_uses_prompts_csv),
        ("mock_full_path", test_mock_provider_full_path),
        ("platform_specific", test_platform_specific_content),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())