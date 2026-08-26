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
import sys
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
        return {"name": p.name}
    stat = p.stat()
    return {
        "name": p.name,
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
    import re
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
        if os.environ.get("ANTHROPIC_API_KEY"):
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
    else:
        text = ""

    parsed = _parse_json_response(text) if text else None
    if parsed and "title" in parsed:
        # Truncate title to limit, strip hashtags
        result = {
            "title": str(parsed["title"])[:title_limit],
            "description": str(parsed.get("description", ""))[:500],
            "hashtags": [
                h.lstrip("#").strip() for h in parsed.get("hashtags", [])[:5] if h
            ],
        }
        return result

    # Fallback to heuristic content (smarter than pure deterministic placeholder)
    return _heuristic_content(video_path, platform)


def batch_generate(video_paths: list[str], platform: str, **kwargs) -> dict[str, dict]:
    """Generate content for multiple videos."""
    return {path: generate_content(path, platform, **kwargs) for path in video_paths}