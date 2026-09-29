"""Content safety predictor.

Scores a video's title + description against learned risk patterns and
optionally an LLM, returning a SafetyVerdict:

    SafetyVerdict(
        score: 0.0..1.0   (higher = safer)
        allow: bool
        reason: str
        flags: list[str]
    )

Two layers:

1. **Heuristic** (always runs): regex/keyword scans for known-bad patterns
   (medical claims, gambling, financial promises, political, etc.). Always
   available, never fails.

2. **LLM** (optional, when API key set): asks the model to rate the content
   on a 1-5 scale. Result is blended with the heuristic.

Used by:
- The `mxpub.py publish` command before each publish: if allow=False, the
  video is marked as `blocked_safety` and skipped.
- The InsightsGenerator for platform-specific risk breakdowns.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional


# Heuristic ban-risk phrases (zh + en). These are NOT the only thing that
# triggers a rejection — they're cheap signals worth a closer look.
_RISKY_PATTERNS = (
    ("medical_claim", ("根治", "治愈", "100%有效", "保证有效", "药到病除",
                        "cure", "guaranteed cure", "100% effective")),
    ("gambling", ("赌博", "博彩", "casino", "bet", "彩票", "百家乐",
                   "blackjack")),
    ("financial_promise", ("稳赚", "无风险", "保本", "高回报", "日赚",
                            "guaranteed profit", "risk-free", "high return",
                            "get rich")),
    ("political", ("politics", "政治", "时政", "领导人", "习近平",
                    "国家领导人")),
    ("adult", ("adult", "色情", "porn", "成人内容", "18禁", "nsfw")),
    ("violence", ("暴力", "血腥", "kill", "murder", "恐怖袭击",
                   "terrorism")),
    ("misleading", ("震惊", "不转不是中国人", "99%的人不知道",
                     "shocking", "clickbait")),
    ("platform_bait", ("关注我", "点赞", "评论", "follow me",
                        "for more")),
)


@dataclass
class SafetyVerdict:
    score: float = 1.0
    allow: bool = True
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    layer: str = "heuristic"   # heuristic / llm / blended

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "allow": self.allow,
            "reason": self.reason,
            "flags": self.flags,
            "layer": self.layer,
        }


def heuristic_score(title: str, description: str = "") -> SafetyVerdict:
    """Pattern-only safety check. Always available, never raises."""
    text = f"{title or ''}\n{description or ''}".lower()
    flags = []
    score = 1.0
    for label, patterns in _RISKY_PATTERNS:
        for p in patterns:
            if p.lower() in text:
                flags.append(f"{label}:{p}")
                # Heavier weight: most platforms auto-reject these categories
                # on the first hit. One hit in a hard-ban category alone is
                # enough to block.
                score -= 0.6 if label in ("medical_claim", "gambling",
                                            "adult", "violence",
                                            "political") else 0.35
                break
    score = max(0.0, score)
    allow = score >= 0.5
    reason = "ok"
    if flags:
        reason = f"heuristic flags: {', '.join(sorted(set(flags))[:5])}"
    return SafetyVerdict(score=score, allow=allow, reason=reason,
                          flags=flags, layer="heuristic")


def llm_score(title: str, description: str = "",
              *, platform: str = "douyin",
              model: Optional[str] = None) -> Optional[SafetyVerdict]:
    """Ask an LLM to rate content safety. Returns None if no API key or error."""
    if not (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")):
        return None
    prompt = (
        f"请评估以下视频内容在 {platform} 平台发布的合规与封号风险。\n"
        "只返回一行严格 JSON，schema: "
        '{"score": 0.0-1.0, "allow": true/false, "reason": "..."}\n'
        f"标题: {title}\n描述: {(description or '')[:400]}\n"
    )
    try:
        if os.environ.get("ANTHROPIC_API_KEY"):
            from ai.content import _call_claude
            raw = _call_claude(prompt,
                                model=model or "claude-haiku-4-5-20251001")
        else:
            from ai.content import _call_openai
            raw = _call_openai(prompt,
                                model=model or "gpt-4o-mini")
        # crude parse: find {"score":...}
        m = re.search(r"\{[^{}]*\"score\"[^{}]*\}", raw)
        if not m:
            return None
        import json
        d = json.loads(m.group(0))
        return SafetyVerdict(
            score=float(d.get("score", 0.5)),
            allow=bool(d.get("allow", True)),
            reason=str(d.get("reason", ""))[:200],
            flags=[f"llm:{platform}"],
            layer="llm",
        )
    except Exception:
        return None


def predict(title: str, description: str = "", *,
            platform: str = "douyin",
            use_llm: bool = True) -> SafetyVerdict:
    """Heuristic + (optional) LLM. Use this from the orchestrator."""
    h = heuristic_score(title, description)
    if not use_llm:
        return h
    llm = llm_score(title, description, platform=platform)
    if llm is None:
        return h
    # Blend: take the minimum score and the stricter allow.
    blended_score = min(h.score, llm.score)
    blended_allow = h.allow and llm.allow
    flags = h.flags + llm.flags
    reason = ("blend: heuristic=" + h.reason
              + f" llm={llm.reason}")
    return SafetyVerdict(score=blended_score, allow=blended_allow,
                          reason=reason, flags=flags, layer="blended")


# ---------------------------------------------------------------------------
# Hook integration
# ---------------------------------------------------------------------------

def install_hook():
    """Register a `video:before_publish` hook that gates unsafe content."""
    from core.hooks import Hooks
    from core.errors import ErrorClass, Action

    def _gate(**kw):
        video = kw.get("video") or {}
        platform = kw.get("platform", "")
        title = video.get("title", "")
        desc = video.get("description", "")
        verdict = predict(title, desc, platform=platform)
        if not verdict.allow:
            # Stash the verdict on the video so the platform module can
            # surface it in the PublishResult.error.
            video["_safety_verdict"] = verdict.to_dict()
            # Emit an on_error so it shows up in stats/risk.
            from core.errors import Classification
            Hooks.emit("video:on_error", video=video,
                        platform=platform,
                        classification=Classification(
                            cls=ErrorClass.CONTENT,
                            action=Action.SKIP_VIDEO,
                            severity=4, cooldown_s=0.0,
                            label="safety_gate",
                            note=verdict.reason))

    Hooks.register("video:before_publish", _gate)


# Auto-install if MX_SAFETY=1 (off by default to preserve current behavior).
if os.environ.get("MX_SAFETY", "0") == "1":
    install_hook()
