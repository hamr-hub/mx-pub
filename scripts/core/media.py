"""Media metadata helpers shared by the batch driver and curation gates."""
from __future__ import annotations

import json
import shutil
import subprocess

FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

# Account rule (2026-09-15): clips shorter than this are never posted to any
# platform. Weixin silently rejects <3s uploads; the user set the floor at 5s.
MIN_DURATION_S = 5.0


def duration_seconds(path: str) -> float:
    """Container duration in seconds; 0.0 when it cannot be determined."""
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return 0.0
        return float(json.loads(proc.stdout).get("format", {}).get("duration") or 0.0)
    except Exception:
        return 0.0


def is_publishable_duration(path: str, floor: float = MIN_DURATION_S) -> tuple[bool, float]:
    """Return (allowed, duration_seconds). Unknown duration is allowed
    (fail-open) so a missing ffprobe never blocks real posting."""
    dur = duration_seconds(path)
    if dur <= 0.0:
        return True, dur
    return dur >= floor, dur
