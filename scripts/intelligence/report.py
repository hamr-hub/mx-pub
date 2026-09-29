"""AI-driven insights report.

Reads the StatsStore + RiskMonitor and produces a structured report with:
- Per-platform health summary
- Best/worst time windows
- Top errors and recommended actions
- Concrete pacing recommendations

If an LLM API key is set (ANTHROPIC_API_KEY / OPENAI_API_KEY), the report is
also sent to the LLM with a prompt asking for "what should we change?". The
LLM response is appended as an "ai_suggestions" section.

Otherwise the report falls back to deterministic templates so you always get
something useful.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from self_learn.stats import StatsStore
from self_learn.optimizer import StrategyOptimizer, clamp_pacing
from self_learn.risk import RiskMonitor


@dataclass
class InsightsReport:
    generated_at: float = 0.0
    platforms: dict = field(default_factory=dict)
    global_warnings: list[str] = field(default_factory=list)
    ai_suggestions: str = ""
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "platforms": self.platforms,
            "global_warnings": self.global_warnings,
            "ai_suggestions": self.ai_suggestions,
            "summary": self.summary,
        }

    def to_markdown(self) -> str:
        lines = [f"# mx-pub 智能洞察报告 ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.generated_at))})"]
        lines.append("")
        if self.summary:
            lines.append("## 概览")
            lines.append("")
            lines.append(self.summary)
            lines.append("")
        if self.global_warnings:
            lines.append("## 全局告警")
            for w in self.global_warnings:
                lines.append(f"- ⚠️  {w}")
            lines.append("")
        for plat, info in self.platforms.items():
            lines.append(f"## {plat}")
            lines.append("")
            for k, v in info.items():
                if isinstance(v, list):
                    if not v:
                        continue
                    lines.append(f"- **{k}**:")
                    for item in v:
                        lines.append(f"  - {item}")
                elif isinstance(v, dict):
                    lines.append(f"- **{k}**: {json.dumps(v, ensure_ascii=False)}")
                else:
                    lines.append(f"- **{k}**: {v}")
            lines.append("")
        if self.ai_suggestions:
            lines.append("## AI 建议")
            lines.append("")
            lines.append(self.ai_suggestions)
            lines.append("")
        return "\n".join(lines)


class InsightsGenerator:
    def __init__(self, store: StatsStore):
        self.store = store
        self.optimizer = StrategyOptimizer(store)
        self.risk = RiskMonitor(store)

    def generate(self, platforms: list[str]) -> InsightsReport:
        report = InsightsReport(generated_at=time.time())
        report_lines = []
        all_ok = True
        for plat in platforms:
            rec = self.optimizer.recommend(plat)
            risk = self.risk.check(plat)
            info = {
                "success_rate": f"{risk.success_rate:.0%}",
                "sample_size": risk.sample_size,
                "risk_level": risk.level,
                "triggers": risk.triggers,
                "recommend_action": risk.recommend_action.value,
                "cooldown_s": risk.cooldown_s,
                "best_hour": (f"{rec.best_hour:02d}:00"
                               if rec.best_hour is not None else "未知"),
                "avoid_hours": [f"{h:02d}:00" for h in rec.avoid_hours],
                # Show what the batch will actually adopt, not the raw
                # advice: _apply_learned_pacing() pushes the clamped values
                # into the pacer, so reporting unclamped numbers here would
                # have the operator reading a 16s interval while the system
                # runs at the 30s floor.
                "pacing_recommendation": clamp_pacing(rec.pacing),
                "warnings": rec.warnings,
                "insights": rec.insights,
            }
            report.platforms[plat] = info
            if risk.level == "critical":
                all_ok = False
                report.global_warnings.append(
                    f"{plat} 风险等级 critical（成功率 {risk.success_rate:.0%}）；建议暂停并人工 review")
            elif risk.level == "warn":
                report.global_warnings.append(
                    f"{plat} 风险等级 warn：{'; '.join(risk.triggers) or '请关注'}")
            if rec.warnings:
                for w in rec.warnings:
                    report_lines.append(f"  - {plat}: {w}")
            if rec.insights:
                for ins in rec.insights:
                    report_lines.append(f"  - {plat}: {ins}")

        if all_ok and report_lines == []:
            report.summary = "所有平台状态正常，未检测到异常模式。"
        else:
            report.summary = "\n".join(report_lines) or "需要人工 review。"

        report.ai_suggestions = self._maybe_llm(report)
        return report

    def _maybe_llm(self, report: InsightsReport) -> str:
        """If an LLM key is set, ask for actionable suggestions. Else ""."""
        prompt = self._build_llm_prompt(report)
        try:
            if os.environ.get("ANTHROPIC_API_KEY"):
                from ai.content import _call_claude
                return _call_claude(
                    prompt,
                    system=("你是 mx-pub 的发布策略顾问。请基于提供的历史数据"
                            "给出 3-5 条具体可执行的优化建议（中文，简洁，"
                            "不要泛泛而谈；只返回 markdown 列表）。"),
                    model=os.environ.get("MX_INSIGHTS_MODEL",
                                          "claude-haiku-4-5-20251001"),
                )
            if os.environ.get("OPENAI_API_KEY"):
                from ai.content import _call_openai
                return _call_openai(
                    prompt,
                    system=("你是 mx-pub 的发布策略顾问。请基于提供的历史数据"
                            "给出 3-5 条具体可执行的优化建议（中文，简洁，"
                            "不要泛泛而谈；只返回 markdown 列表）。"),
                    model=os.environ.get("MX_INSIGHTS_MODEL", "gpt-4o-mini"),
                )
        except Exception as e:
            return f"(LLM 调用失败，使用模板建议)\n\n{e}"
        return ""

    def _build_llm_prompt(self, report: InsightsReport) -> str:
        parts = ["以下是一个多平台视频自动发布系统的近期表现数据，请基于此给出具体的优化建议：\n"]
        for plat, info in report.platforms.items():
            parts.append(f"\n### {plat}")
            for k, v in info.items():
                parts.append(f"- {k}: {v}")
        if report.global_warnings:
            parts.append("\n### 全局告警")
            for w in report.global_warnings:
                parts.append(f"- {w}")
        return "\n".join(parts)


def write_report(report: InsightsReport, *,
                 md_path: Optional[str] = None,
                 json_path: Optional[str] = None) -> tuple[str, str]:
    """Persist the report. Returns (md_path, json_path)."""
    if md_path is None:
        md_path = os.environ.get("MX_INSIGHTS_MD",
                                  str(Path(__file__).resolve().parent.parent.parent
                                       / "insights_report.md"))
    if json_path is None:
        json_path = os.environ.get("MX_INSIGHTS_JSON",
                                    str(Path(__file__).resolve().parent.parent.parent
                                         / "insights_report.json"))
    Path(md_path).write_text(report.to_markdown(), encoding="utf-8")
    Path(json_path).write_text(json.dumps(report.to_dict(),
                                           indent=2, ensure_ascii=False),
                                encoding="utf-8")
    return md_path, json_path


from pathlib import Path  # late import to avoid cycle at module top
