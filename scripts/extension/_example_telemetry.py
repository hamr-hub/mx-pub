"""Example extension: custom telemetry hook.

Auto-loaded by load_extensions() at startup. The hook fires every time the
core marks a video as successfully published. Real implementations would push
to an external dashboard / DB / alerting system.

Disable by setting MX_TELEMETRY=0 in the environment.
"""
import os

from core.hooks import Hooks


_ENABLED = os.environ.get("MX_TELEMETRY", "1") not in ("0", "false", "no")


def _on_publish(**kwargs):
    if not _ENABLED:
        return
    platform = kwargs.get("platform", "?")
    video = kwargs.get("video") or {}
    result = kwargs.get("result")
    status = getattr(result, "status", "?") if result else "?"
    print(f"[telemetry] {platform}/{status} {video.get('title', '')[:40]}")


def _on_error(**kwargs):
    if not _ENABLED:
        return
    classification = kwargs.get("classification")
    cls_name = classification.cls.value if classification else "?"
    sev = classification.severity if classification else 0
    print(f"[telemetry] error cls={cls_name} sev={sev}")


if _ENABLED:
    Hooks.register("video:after_publish", _on_publish)
    Hooks.register("video:on_error", _on_error)
