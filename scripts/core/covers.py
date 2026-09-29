"""Cover-image generation for platform post forms.

Horizontal (4:3) and vertical (3:4) covers are center-cropped from one source
frame. Source clips are landscape 16:9, so width is cropped (height preserved):
- 4:3 -> 1440x1080, crop x offset 240
- 3:4 -> 810x1080,  crop x offset 555
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

COVER_DIR = Path("/tmp/mx_covers")
FRAME_AT_S = 1
COVER_W_H = 1440
COVER_H_H = 1080
COVER_W_V = 810
COVER_H_V = 1080

# First force-fill a 16:9 canvas so the center-crop offsets are valid even if
# a future clip ships in a different aspect.
TO_16x9 = "scale=1920:1080"
HF = (f"{TO_16x9},crop={COVER_W_H}:{COVER_H_H}:240:0")
VF = (f"{TO_16x9},crop={COVER_W_V}:{COVER_H_V}:555:0")


def _slug(video_path: str) -> str:
    return hashlib.md5(video_path.encode()).hexdigest()[:12]


def make_covers(video_path: str) -> tuple[str, str]:
    """Render (horizontal, vertical) cover JPEGs; cached by source path."""
    stem = _slug(str(video_path))
    h_path = COVER_DIR / f"{stem}_h.jpg"
    v_path = COVER_DIR / f"{stem}_v.jpg"
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    jobs = ((h_path, HF), (v_path, VF))
    for out, vf in jobs:
        if out.exists():
            continue
        cmd = ["ffmpeg", "-y", "-ss", str(FRAME_AT_S), "-i", str(video_path),
               "-vf", vf, "-frames:v", "1", "-q:v", "3", str(out)]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(
                f"cover render failed {video_path}: "
                f"{proc.stderr.decode()[-200:]}")
    return str(h_path), str(v_path)
