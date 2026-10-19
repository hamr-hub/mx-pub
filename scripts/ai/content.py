"""AI-powered content generation for video publishing.

Supports multiple AI providers:
- Claude (Anthropic API) — recommended
- OpenAI (gpt-4o-mini)
- Local Ollama (llama3.2, qwen2, etc.)

Auto-generates platform-specific content based on video metadata or
file properties. Falls back to deterministic placeholder content if
no AI provider is configured.

Environment variables:
    ANTHROPIC_API_KEY   - Claude API key
    OPENAI_API_KEY      - OpenAI API key
    OLLAMA_HOST         - Ollama host (default http://localhost:11434)

Usage:
    from ai.content import generate_content
    content = generate_content(video_path, platform='xhs')
    # Returns: {title, description, hashtags}
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# Per-platform title limits
TITLE_LIMITS = {
    "xhs": 20,
    "xiaohongshu": 20,
    "douyin": 30,
    "kuaishou": 30,
    "weixin": 14,
    "weixin_channels": 14,
}

# Per-platform style hints
PLATFORM_STYLE = {
    "xhs": "小红书风格：文艺、感性、emoji点缀、话题标签吸引年轻女性",
    "xiaohongshu": "小红书风格：文艺、感性、emoji点缀、话题标签吸引年轻女性",
    "douyin": "抖音风格：口语化、节奏紧凑、悬念开场、强情绪共鸣",
    "kuaishou": "快手风格：朴实、生活化、接地气、强代入感",
    "weixin": "视频号风格：正式、简短、新闻式标题",
    "weixin_channels": "视频号风格：正式、简短、新闻式标题",
}


def _read_video_metadata(video_path: str) -> dict:
    """Extract basic metadata from video file (size, mtime)."""
    p = Path(video_path)
    if not p.exists():
        return {"name": p.name, "path": str(p)}
    stat = p.stat()
    return {
        "name": p.name,
        # The full path matters: the media tree holds several files with the
        # same basename but different prompts.csv seeds, and a name-only prompt
        # made the lookup resolve to whichever one was found first.
        "path": str(p),
        "size_mb": round(stat.st_size / 1024 / 1024, 1),
    }


def _extract_video_thumbnail_b64(video_path: str) -> Optional[str]:
    """Try to extract a thumbnail from the video (for AI vision)."""
    # Use ffmpeg if available
    try:
        import subprocess
        import base64
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ss", "00:00:01",
             "-frames:v", "1", "-q:v", "2", tmp_path],
            capture_output=True, timeout=10,
        )
        if Path(tmp_path).exists() and Path(tmp_path).stat().st_size > 0:
            with open(tmp_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
    except Exception:
        pass
    return None


def _call_claude(prompt: str, system: str = "", model: str = "claude-haiku-4-5-20251001") -> str:
    """Call Claude API."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        kwargs = {"model": model, "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
        return msg.content[0].text
    except Exception as e:
        raise RuntimeError(f"claude_api: {e}")


def _call_openai(prompt: str, system: str = "", model: str = "gpt-4o-mini") -> str:
    """Call OpenAI API."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(model=model, messages=messages, max_tokens=500)
        return resp.choices[0].message.content
    except Exception as e:
        raise RuntimeError(f"openai_api: {e}")


def _call_ollama(prompt: str, system: str = "", model: str = "llama3.2") -> str:
    """Call local Ollama."""
    try:
        import urllib.request
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        body = json.dumps({"model": model, "prompt": prompt, "system": system, "stream": False}).encode()
        req = urllib.request.Request(f"{host}/api/generate", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data.get("response", "")
    except Exception as e:
        raise RuntimeError(f"ollama: {e}")


#: Resolved once — both candidates usually point at the same tree, and the
#: images directory holds ~60k entries on this machine.
_VIDEO_SEARCH_ROOTS: list[Path] = [
    Path("/Users/hyx/ssd/codespace/personal/images"),
    Path("~/ssd/codespace/personal/images").expanduser(),
]

#: filename -> resolved path (or None). Without this, generating content for
#: a batch re-walked the whole tree once per video.
_video_lookup_cache: dict[str, Optional[str]] = {}

#: Directories that never contain source media and are expensive to walk.
_SKIP_DIRS = {".git", ".Trash", ".svn", "__pycache__", "node_modules",
              ".cache", "Library"}


#: {filename: path} for every media file under the roots, built lazily on the
#: first lookup that has to fall back to a tree walk.
_video_index: Optional[dict[str, str]] = None

_MEDIA_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mpg", ".mpeg")


def _build_video_index(*, max_depth: int = 6) -> dict[str, str]:
    """One walk of the media roots, recording every file by basename.

    Built at most once per process. The previous code re-walked the whole tree
    for every video (60k entries here), twice over, because both search roots
    resolve to the same directory — so generating content for a batch cost
    minutes. Ambiguous basenames keep their first (shallowest) match.
    """
    global _video_index
    if _video_index is not None:
        return _video_index

    index: dict[str, str] = {}
    seen_roots: set[Path] = set()
    for root in _VIDEO_SEARCH_ROOTS:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen_roots or not resolved.is_dir():
            continue
        seen_roots.add(resolved)
        root_depth = len(resolved.parts)
        for dirpath, dirnames, filenames in os.walk(resolved):
            here = Path(dirpath)
            if len(here.parts) - root_depth >= max_depth:
                dirnames[:] = []          # prune deeper levels
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if name.lower().endswith(_MEDIA_SUFFIXES):
                    index.setdefault(name, str(here / name))
    _video_index = index
    return index


def _resolve_video_path(filename: str) -> Optional[str]:
    """Resolve the video the prompt refers to.

    The prompt usually carries the full path the caller already had, so try it
    directly before touching the tree. Falling straight through to a basename
    search is wrong: the media tree holds several `video_1.mp4` files with
    different prompts.csv seeds, and first-match-wins silently picked the
    wrong one.
    """
    if not filename:
        return None
    candidate = Path(filename.strip())
    if len(candidate.parts) > 1:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            pass
    return _find_source_video(filename)


def _find_source_video(filename: str) -> Optional[str]:
    """Locate a source video by name under the known media roots.

    Cached per filename, and backed by a single lazily-built index so a batch
    costs one tree walk in total rather than one per video.
    """
    if not filename:
        return None
    if filename in _video_lookup_cache:
        return _video_lookup_cache[filename]

    target = Path(filename).name
    index = _build_video_index()
    found = index.get(target)
    if found is None:
        _video_lookup_cache[filename] = None
        return None

    # Several files can share a basename. If the caller named a path, prefer
    # the candidate sharing the deepest directory prefix with it.
    wanted = Path(filename.strip()).parent
    if len(wanted.parts) > 1:
        wanted_parts = wanted.parts
        best, best_score = found, -1
        for name, path in index.items():
            if name != target:
                continue
            score = 0
            # Prefix comparison: the two paths have different depths.
            for a, b in zip(wanted_parts,  # noqa: B905
                           Path(path).parent.parts):
                if a != b:
                    break
                score += 1
            if score > best_score:
                best, best_score = path, score
        found = best

    _video_lookup_cache[filename] = found
    return found


def _reset_video_lookup_cache() -> None:
    """Test seam: drop both the memo and the index."""
    global _video_index
    _video_lookup_cache.clear()
    _video_index = None


def _call_mock(prompt: str, system: str = "", model: str = "mock") -> str:
    """Mock AI provider for testing. Generates platform-appropriate content
    deterministically from prompts.csv topic_seed. Set MOCK_AI=1 to enable.

    This proves the full code path works without needing real API keys.
    """
    import re as _re

    # Extract filename and platform style
    # Prefer the full path; fall back to the bare name.
    m = _re.search(r"视频路径: ([^\n]+)", prompt)
    filename = m.group(1).strip() if m else ""
    if not filename:
        m = _re.search(r"视频文件名: ([^\n]+)", prompt)
        filename = m.group(1).strip() if m else ""
    m = _re.search(r"平台风格: ([^\n]+)", prompt)
    style = m.group(1).strip() if m else ""
    m = _re.search(r"标题字数限制: (\d+)", prompt)
    title_limit = int(m.group(1)) if m else 30

    video_path = _resolve_video_path(filename)

    # Get the real descriptive content from prompts.csv
    seed = _read_prompt_for_video(video_path) or "" if video_path else ""

    if seed:
        # Use first descriptive phrase (before comma) as title
        title_words = seed.split(",")[0].strip()
        # Strip leading fluff
        for fluff in ["cinematic", "editorial", "photorealistic", "8K", "4K", "high dynamic range"]:
            title_words = _re.sub(rf'^{fluff}\s+', '', title_words, flags=_re.IGNORECASE)
    else:
        name = Path(filename).stem if filename else "video"
        title_words = _re.sub(r'^video\s*\d+\s*', '', name.replace("_", " ").replace("-", " "),
                             flags=_re.IGNORECASE).strip() or "精彩视频"

    # Style-driven descriptions
    if "文艺" in style:
        description = f"{title_words}的每一帧都美到窒息～ 忍不住循环播放✨"
    elif "douyin" in style.lower() or "抖音" in style:
        description = f"{title_words}！前2秒就让人移不开眼🔥"
    elif "kuaishou" in style.lower() or "快手" in style:
        description = f"老铁们看这个{title_words}，太真实了👍"
    elif "正式" in style or "weixin" in style.lower():
        description = title_words
    else:
        description = title_words

    # Keyword extraction from seed
    keywords_map = {
        "lightning": "lightning", "storm": "storm", "thunder": "thunder",
        "manhattan": "nyc", "tokyo": "tokyo", "shibuya": "shibuya",
        "car": "car", "drift": "drift", "luxury": "luxury",
        "duel": "duel", "rain": "rain",
        "cherry": "sakura", "blossom": "sakura", "timelapse": "timelapse",
        "kyoto": "kyoto", "butterfly": "butterfly", "macro": "macro",
        "aurora": "aurora", "iceland": "iceland", "campfire": "campfire",
        "fire": "fire", "mountain": "mountain", "tiger": "tiger",
        "wildlife": "wildlife", "hummingbird": "hummingbird",
        "underwater": "ocean", "ocean": "ocean", "coral": "ocean",
        "angkor": "temple", "temple": "temple",
        "neon": "cyberpunk", "cyberpunk": "cyberpunk",
        "samurai": "samurai",
    }
    tags = []
    seen = set()
    haystack = (seed + " " + title_words).lower()
    for k, v in keywords_map.items():
        if k in haystack and v not in seen:
            tags.append(v)
            seen.add(v)
    if not tags:
        tags = ["video", "share"]
    tags = tags[:5]

    return json.dumps({
        "title": title_words[:title_limit],
        "description": description,
        "hashtags": tags,
    }, ensure_ascii=False)


def _parse_json_response(text: str) -> Optional[dict]:
    """Parse JSON from AI response (may be wrapped in markdown)."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        return json.loads(text)
    except Exception:
        # Try to find JSON in the text
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                pass
    return None


def _extract_keywords_from_name(name: str) -> list[str]:
    """Extract candidate keywords from video filename."""
    # Common keywords mapping for visual themes
    keyword_map = {
        "lightning": ["#lightning", "#storm"],
        "storm": ["#storm", "#weather"],
        "thunder": ["#thunder", "#storm"],
        "manhattan": ["#nyc", "#city"],
        "skyline": ["#skyline", "#city"],
        "tokyo": ["#tokyo", "#japan", "#street"],
        "shibuya": ["#tokyo", "#shibuya", "#night"],
        "car": ["#car", "#drift"],
        "drift": ["#drift", "#car", "#night"],
        "sports": ["#car", "#racing"],
        "luxury": ["#luxury", "#car"],
        "samurai": ["#samurai", "#cinematic", "#duel"],
        "duel": ["#duel", "#cinematic", "#samurai"],
        "bamboo": ["#bamboo", "#forest", "#cinematic"],
        "rain": ["#rain", "#cinematic"],
        "cinematic": ["#cinematic", "#film"],
        "cherry": ["#sakura", "#cherry", "#kyoto", "#timelapse"],
        "blossom": ["#sakura", "#cherry"],
        "timelapse": ["#timelapse", "#nature"],
        "kyoto": ["#kyoto", "#japan"],
        "butterfly": ["#butterfly", "#macro", "#nature"],
        "monarch": ["#butterfly", "#macro"],
        "macro": ["#macro", "#nature"],
        "aurora": ["#aurora", "#northern", "#night"],
        "iceland": ["#iceland", "#aurora", "#cabin"],
        "cabin": ["#cabin", "#aurora"],
        "astronaut": ["#space", "#mars", "#astronaut"],
        "mars": ["#mars", "#space"],
        "tiger": ["#tiger", "#wildlife", "#jungle"],
        "wildlife": ["#wildlife", "#nature"],
        "hummingbird": ["#hummingbird", "#wildlife", "#macro"],
        "hibiscus": ["#flower", "#nature"],
        "campfire": ["#campfire", "#fire", "#night", "#mountain"],
        "mountain": ["#mountain", "#nature"],
        "fire": ["#fire", "#campfire"],
        "ember": ["#fire", "#night"],
        "corals": ["#ocean", "#coral"],
        "underwater": ["#ocean", "#underwater"],
        "biolumines": ["#ocean", "#underwater"],
        "angels": ["#temple", "#ancient"],
        "angkor": ["#temple", "#ancient"],
        "maple": ["#japan", "#autumn", "#kyoto"],
        "autumn": ["#autumn", "#fall"],
        "temple": ["#temple", "#ancient", "#asia"],
        "warrior": ["#samurai", "#warrior"],
    }

    lower = name.lower()
    found = []
    seen = set()
    for key, tags in keyword_map.items():
        if key in lower and not any(t in seen for t in tags):
            for t in tags:
                if t not in seen:
                    found.append(t)
                    seen.add(t)
            if len(found) >= 4:
                break
    return found[:5]


def _read_prompt_for_video(video_path: str) -> Optional[str]:
    """Look up the topic_seed for this video in prompts.csv.

    Walks up to find a prompts.csv in the same directory. Matches by filename.
    """
    import csv
    p = Path(video_path)
    candidates = [p.parent / "prompts.csv", p.parent.parent / "prompts.csv"]
    filename = p.name
    for csv_path in candidates:
        if not csv_path.exists():
            continue
        try:
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    file_path = row.get("file_path", "")
                    if file_path.endswith(filename) or filename in file_path:
                        seed = row.get("topic_seed", "").strip()
                        if seed:
                            return seed
        except Exception:
            continue
    return None


def _heuristic_content(video_path: str, platform: str) -> dict:
    """Heuristic content generation when no AI API is available.

    Reads topic_seed from prompts.csv for actual descriptive content.
    Better than pure deterministic placeholder.
    """
    seed = _read_prompt_for_video(video_path) or ""

    title_limit = TITLE_LIMITS.get(platform, 30)
    platform_emojis = {
        "xhs": ["✨", "🌸", "💫", "🌟", "🌈", "💖"],
        "xiaohongshu": ["✨", "🌸", "💫"],
        "douyin": ["🔥", "⚡", "💥", "🎬"],
        "kuaishou": ["👍", "❤️", "💪"],
        "weixin": [],
        "weixin_channels": [],
    }
    emojis = platform_emojis.get(platform, [""])

    import re as _re
    if not seed:
        # Fallback: extract from filename
        name = Path(video_path).stem
        title_words = _re.sub(r'^video\s*\d+\s*', '', name.replace("_", " ").replace("-", " "),
                             flags=_re.IGNORECASE).strip() or "精彩瞬间"
    else:
        # Extract first descriptive phrase (before comma or up to 40 chars)
        title_words = seed.split(",")[0].strip()
        # Remove leading cinematic/editorial fluff for shorter titles
        for fluff in ["cinematic", "editorial", "photorealistic", "8K", "4K", "high dynamic range", "macro time-collapse"]:
            title_words = _re.sub(rf'^{fluff}\s+', '', title_words, flags=_re.IGNORECASE)

    if platform in ("weixin", "weixin_channels"):
        title = title_words[:title_limit]
    elif platform in ("xhs", "xiaohongshu"):
        title = f"{emojis[0]} {title_words}"[:title_limit].strip()
    elif platform == "douyin":
        title = f"{title_words} {emojis[0]}"[:title_limit].strip()
    else:
        title = title_words[:title_limit]

    # Hashtags from seed keywords
    hashtags = _extract_keywords_from_name(seed + " " + Path(video_path).stem)
    if not hashtags:
        hashtags = [f"#{platform}", "#video", "#share"]

    # Description: use topic_seed (truncated) with platform style
    if seed:
        # Take first 2 clauses
        clauses = seed.split(",")
        description = ", ".join(clauses[:3]).strip()[:200]
    else:
        description = title_words

    style_suffix = {
        "xhs": "～ 喜欢吗？",
        "xiaohongshu": "～ 喜欢吗？",
        "douyin": " 完整看更震撼🔥",
        "kuaishou": " 老铁双击👍",
        "weixin": "",
        "weixin_channels": "",
    }.get(platform, "")

    if style_suffix:
        description = (description + style_suffix)[:500]

    return {
        "title": title,
        "description": description,
        "hashtags": [h.lstrip("#") for h in hashtags],
    }


def generate_content(video_path: str, platform: str = "xhs",
                    provider: Optional[str] = None,
                    model: Optional[str] = None) -> dict:
    """Generate platform-specific content for a video.

    Args:
        video_path: path to the video file
        platform: target platform (xhs, douyin, kuaishou, weixin)
        provider: 'claude', 'openai', 'ollama', or None for auto-detect
        model: model name (default for each provider)

    Returns:
        dict with keys: title, description, hashtags (list of strings without #)
    """
    # Auto-detect provider
    if not provider:
        if os.environ.get("MOCK_AI") in ("1", "true", "yes"):
            provider = "mock"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "claude"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("OLLAMA_HOST") or Path("/usr/local/bin/ollama").exists():
            provider = "ollama"

    title_limit = TITLE_LIMITS.get(platform, 30)
    style = PLATFORM_STYLE.get(platform, "通用风格")
    meta = _read_video_metadata(video_path)

    # Try AI providers
    prompt = f"""为以下视频生成{platform}平台的发布文案。

视频文件名: {meta.get('name', '')}
视频路径: {meta.get('path', '')}
视频大小: {meta.get('size_mb', '?')} MB
平台风格: {style}
标题字数限制: {title_limit} 字以内

请以 JSON 格式输出（不要 markdown，不要代码块）：
{{
  "title": "不超过 {title_limit} 字的吸引人标题",
  "description": "50-200 字视频描述，调动情绪/好奇心",
  "hashtags": ["标签1", "标签2", "标签3", "标签4", "标签5"]
}}
"""

    if provider == "claude":
        try:
            text = _call_claude(prompt, system="你是短视频发布专家，输出必须是合法 JSON。", model=model or "claude-haiku-4-5-20251001")
        except Exception as e:
            print(f"  [ai] Claude failed: {e}, falling back")
            text = ""
    elif provider == "openai":
        try:
            text = _call_openai(prompt, system="你是短视频发布专家，输出必须是合法 JSON。", model=model or "gpt-4o-mini")
        except Exception as e:
            print(f"  [ai] OpenAI failed: {e}, falling back")
            text = ""
    elif provider == "ollama":
        try:
            text = _call_ollama(prompt, system="你是短视频发布专家，输出必须是合法 JSON。", model=model or "llama3.2")
        except Exception as e:
            print(f"  [ai] Ollama failed: {e}, falling back")
            text = ""
    elif provider == "mock":
        text = _call_mock(prompt, system="你是短视频发布专家，输出必须是合法 JSON。")
    else:
        text = ""

    parsed = _parse_json_response(text) if text else None
    if isinstance(parsed, dict):
        # A blank title is not a usable result: it would publish a post with no
        # title at all. LLM output regularly contains `"title": ""`.
        title = str(parsed.get("title") or "").strip()
        if title:
            raw_tags = parsed.get("hashtags") or []
            if isinstance(raw_tags, str):      # tolerate a single string
                raw_tags = [raw_tags]
            result = {
                "title": title[:title_limit],
                "description": str(parsed.get("description") or "")[:500],
                "hashtags": [
                    str(h).lstrip("#").strip()
                    for h in list(raw_tags)[:5] if str(h or "").strip()
                ],
            }
            return result
        # else: fall through to the heuristic path below

    # Fallback to heuristic content (smarter than pure deterministic placeholder)
    return _heuristic_content(video_path, platform)


def batch_generate(video_paths: list[str], platform: str, **kwargs) -> dict[str, dict]:
    """Generate content for multiple videos."""
    return {path: generate_content(path, platform, **kwargs) for path in video_paths}
