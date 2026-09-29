#!/usr/bin/env python3
"""Build 二创 (derivative) cuts from validated high-VV own works.

Why: the KS work history shows five self-owned AI topics repeatedly hit the
~3k distribution ceiling (car drift, bamboo samurai, tuna chef, biolum reef,
hummingbird). Re-uploading the same file is useless (platform dup detection);
these derivatives are genuinely re-edited cuts:

  variant A — cinematic slow cut: 1.30x slow, grade + vignette, fades, hook
              title card (~7.6s from a 5.875s source)
  variant B — dual-render recut: two DIFFERENT renders of the same prompt
              joined with a crossfade and one unified grade/title (~11-14s)

All outputs are re-encoded (libx264, no audio pass-through), >=5s per the
account floor, and land in artifacts/derivatives/out (git-ignored). The run
also emits a curation spec (same schema as curations/*.json) carrying
`derivative: true` + source metadata so build_themed_queue can merge it while
bypassing the live-topic duplicate gate (the topic is deliberately revisited).

Usage:
  python3 scripts/make_derivatives.py \
      artifacts/derivatives/src/sources.json curations/20260915_derivatives.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
FONT = "/System/Library/Fonts/PingFang.ttc"
W, H, FPS = 1280, 720, 30
SLOW = 1.30

# Per-topic editorial: (topic key, source selection for A, pair for B, copy)
PLANS = [
    {
        "key": "car",
        "match": "sports car drift",
        "collection": "奇幻视界",
        "a": {"id": 206909622983, "title": "涩谷雨夜漂移",
              "desc": "雨夜涩谷路口,豪车甩尾过弯,霓虹在湿地上碎成光河。AI生成科幻短片"},
        "b": {"ids": [206909622983, 206908610564], "title": "双视角雨夜漂移",
              "desc": "两台摄影机位的雨夜涩谷漂移,交叉剪辑把霓虹与水雾连成一场梦。AI生成科幻短片"},
        "hashtags": ["跑车", "漂移", "赛博朋克", "雨夜", "AI短片"],
        "topic": "luxury sports car drift on wet Shibuya crossing at night neon reflections",
    },
    {
        "key": "samurai",
        "match": "samurai duel",
        "collection": "奇幻视界",
        "a": {"id": 206899969843, "title": "竹林雨中决斗",
              "desc": "竹林急雨,两位兜帽武士拔刀相向,风过竹梢,胜负只在一瞬。AI生成武侠短片"},
        "b": {"ids": [206899969843, "LOCAL_ARK"], "title": "竹林剑客双幕",
              "desc": "竹林雨中对决的两次演绎剪在一起:一刀的静止,与满林的风声。AI生成武侠短片"},
        "hashtags": ["武侠", "武士", "竹林", "决斗", "AI短片"],
        "topic": "hooded samurai duel in bamboo forest during rain drawn swords",
        "local_extra": {
            "LOCAL_ARK": "/Volumes/ssd/codespace/personal/images/"
                         "20260703test/20260703/ark_agent_plan/video_1.mp4"},
    },
    {
        "key": "sushi",
        "match": "bluefin tuna",
        "collection": "人间烟火",
        "a": {"id": 206909460961, "title": "金枪鱼主厨刀工",
              "desc": "主厨手起刀落,蓝鳍金枪鱼大腹被切成均匀的一片,油花在灯下发亮。AI生成美食短片"},
        "b": {"ids": [206909460961, 206909175074], "title": "蓝鳍金枪双刀工",
              "desc": "两次运镜看同一位主厨拆解蓝鳍金枪鱼:刀面反光,鱼肉肌理分毫毕现。AI生成美食短片"},
        "hashtags": ["美食", "金枪鱼", "刀工", "日料", "AI短片"],
        "topic": "sushi chef slicing bluefin tuna in slow motion close up",
    },
    {
        "key": "reef",
        "match": "underwater dolly",
        "collection": "自然之诗",
        "a": {"id": 206909758031, "title": "荧光珊瑚海潜行",
              "desc": "镜头滑进午夜的荧光珊瑚礁,蓝绿光点随水流明灭,像海底下起了星雨。AI生成自然短片"},
        "b": {"ids": [206909758031, 206910229795], "title": "荧光深海双幕",
              "desc": "两片荧光珊瑚海的潜行镜头接在一起,蓝光越来越深,直到忘记呼吸。AI生成自然短片"},
        "hashtags": ["海底世界", "荧光", "珊瑚", "潜水", "AI短片"],
        "topic": "underwater dolly through bioluminescent coral reef at night",
    },
    {
        "key": "hummingbird",
        "match": "hummingbird hovering",
        "collection": "自然之诗",
        "a": {"id": 206909096415, "title": "朱槿蜂鸟悬停",
              "desc": "蜂鸟在朱槿花前悬停,翅膀化成虚影,细长的喙探入花蕊的一瞬。AI生成自然短片"},
        "b": {"ids": [206909096415, 206910082533], "title": "蜂鸟悬停双视角",
              "desc": "两个机位看蜂鸟悬停采蜜:翅膀每秒振成雾,花与鸟同时静止在空中。AI生成自然短片"},
        "hashtags": ["蜂鸟", "自然", "微距", "花朵", "AI短片"],
        "topic": "hummingbird hovering at tropical hibiscus flower feeding macro",
    },
]


def duration(path: Path) -> float:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=30)
    return float(proc.stdout.strip() or 0)


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _drawtext(text: str, enable: str) -> str:
    """drawtext filter with PingFang; escape ffmpeg filtergraph specials."""
    esc = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return (f"drawtext=fontfile='{FONT}':text='{esc}':fontcolor=white:fontsize=46:"
            f"x=(w-text_w)/2:y=h*0.10:shadowcolor=black@0.75:shadowx=2:shadowy=2:"
            f"enable='{enable}'")


def _grade() -> str:
    return ("scale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,fps=%d,setsar=1,eq=contrast=1.07:saturation=1.18:gamma=1.02,"
            "vignette=PI/5" % (W, H, W, H, FPS))


def build_a(src: Path, out: Path, title: str) -> None:
    """Slow cinematic single-render cut."""
    d = duration(src) * SLOW
    vf = (f"[0:v]{_grade()},setpts={SLOW}*PTS,"
          f"fade=t=in:st=0:d=0.5,fade=t=out:st={d - 0.8:.2f}:d=0.8,"
          f"{_drawtext(title, f'between(t,0.3,{d - 1.0:.2f})')}[v]")
    cmd = [FFMPEG, "-y", "-i", str(src), "-filter_complex", vf,
           "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "medium",
           "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           str(out)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)


def build_b(src1: Path, src2: Path, out: Path, title: str) -> None:
    """Two different renders of one topic, crossfaded into one recut."""
    d1, d2 = duration(src1), duration(src2)
    offset = d1 - 0.8
    total = d1 + d2 - 0.8
    vf = (f"[0:v]{_grade()},setpts=PTS-STARTPTS[a];"
          f"[1:v]{_grade()},setpts=PTS-STARTPTS[b];"
          f"[a][b]xfade=transition=fade:duration=0.8:offset={offset:.3f}[x];"
          f"[x]fade=t=in:st=0:d=0.5,fade=t=out:st={total - 0.8:.2f}:d=0.8,"
          f"{_drawtext(title, f'between(t,0.3,{total - 1.0:.2f})')}[v]")
    cmd = [FFMPEG, "-y", "-i", str(src1), "-i", str(src2),
           "-filter_complex", vf, "-map", "[v]", "-an",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=420)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", type=Path)
    ap.add_argument("spec_out", type=Path)
    args = ap.parse_args()

    src_dir = args.sources.parent
    out_dir = src_dir.parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    by_id = {s["id"]: src_dir / Path(s["file"]).name
             for s in json.loads(args.sources.read_text())}

    videos: list[dict] = []
    for plan in PLANS:
        a_src = by_id[plan["a"]["id"]]
        entries = [("a", plan["a"]["title"], a_src, plan["a"]["desc"])]
        extra = plan.get("local_extra") or {}
        b_paths = []
        for pid in plan["b"]["ids"]:
            if isinstance(pid, str) and pid.startswith("LOCAL_"):
                b_paths.append(Path(extra[pid]))
            else:
                b_paths.append(by_id[pid])
        entries.append(("b", plan["b"]["title"], b_paths, plan["b"]["desc"]))

        for variant, title, srcs, desc in entries:
            stem = f"{plan['key']}_{variant}"
            out = out_dir / f"{stem}.mp4"
            if variant == "a":
                build_a(srcs, out, title)
            else:
                if md5(srcs[0]) == md5(srcs[1]):
                    print(f"⚠️  {stem}: pair renders are the SAME file, "
                          "skipping dual-render cut")
                    continue
                build_b(srcs[0], srcs[1], out, title)
            dur = duration(out)
            if dur < 5.0:
                print(f"❌ {stem}: {dur:.2f}s below floor; not emitting")
                continue
            print(f"✅ {out.name} {dur:.2f}s {title}")
            videos.append({
                "path": str(out.resolve()),
                "collection": plan["collection"],
                "title": title,
                "description": f"{desc} #{' #'.join(plan['hashtags'])}",
                "hashtags": plan["hashtags"],
                "topic": plan["topic"],
                "derivative": True,
                "sources": [s.name if hasattr(s, "name") else str(s)
                            for s in (srcs if variant == "b" else [srcs])],
            })

    spec = {
        "batch": "20260915_derivatives",
        "images_root": str(out_dir.resolve()),
        "derivative": True,
        "collections": {
            "奇幻视界": "AI 生成的科幻与武侠奇境",
            "自然之诗": "AI 生成的自然光影瞬间",
            "人间烟火": "AI 生成的市井与手艺烟火气",
        },
        "videos": videos,
    }
    args.spec_out.write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    print(f"\n📝 spec -> {args.spec_out} ({len(videos)} derivatives)")
    return 0 if videos else 1


if __name__ == "__main__":
    sys.exit(main())
