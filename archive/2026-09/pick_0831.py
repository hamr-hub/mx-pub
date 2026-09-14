#!/usr/bin/env python3
"""
Classify 2026-08-31 prompts by theme, pick 1 best per theme.
Best = highest (prompt_chars * 2 + mtime_24h) score; dedup by base name.
Output: publish_picks_0831.json (skeleton queue) — feeds loop_publish_v2.
"""
import csv
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV = Path("/Volumes/ssd/codespace/personal/images/2026-08-31/logs/platform-cash-batch-summary.csv")
VIDEOS = Path("/Volumes/ssd/codespace/personal/images/2026-08-31/videos")
PICKS_FILE = HERE / "publish_picks_0831.json"

# 主题词表 — 同上,多命中取第一个;沙漠会被埃及抢,所以沙漠用更严苛的词
THEMES = [
    ("古风/中式", ["古风","中式","汉服","水墨","山水","竹林","红灯","灯笼","中式古","古寺","寺庙","红叶","银杏","枫叶","稻田","荷塘"]),
    ("樱花/日本", ["樱花","日本","东京","京都","艺伎","武士","日式","艺妓"]),
    ("古罗马", ["古罗马","罗马","角斗","斗兽","凯撒"]),
    ("古埃及", ["古埃及","埃及","法老","狮身"]),
    ("北欧/维京", ["北欧","维京","极光","峡湾","冰岛","雪屋","冰原"]),
    ("蒸汽朋克", ["蒸汽","齿轮","飞艇","朋克"]),
    ("科幻/未来", ["科幻","宇航","火星","赛博","机器人","飞船","太空","星际"]),
    ("海底/海洋", ["海底","海豚","鲸","鱼群","珊瑚","深海"]),
    ("雪山/极地", ["雪山","冰川","北极","企鹅","冰山"]),
    ("沙漠", ["沙漠","骆驼","沙丘","戈壁"]),
    ("雨林/溪流", ["雨林","瀑布","溪流","小溪","竹海","水潭","河谷","峡谷"]),
    ("森林/自然", ["森林","松林","林间","丛林","密林","树冠"]),
    ("夜景/星空", ["星空","银河","夜景","月光","灯塔"]),
    ("动物/宠物", ["柴犬","猫","狗","蝴蝶","狼","鹰","鸟","熊","鹿","金雕","猛禽","蜻蜓","萤火虫"]),
    ("城市/地标", ["城市","街道","天际线","建筑","城堡","教堂","塔楼","铁塔","地标","彩虹","广场"]),
    ("夕阳/黄昏", ["夕阳","日落","黄昏","余晖"]),
    ("日出/清晨", ["日出","清晨","晨雾","晨光"]),
    ("神秘/奇幻", ["神秘","奇幻","魔法","精灵","龙"]),
    ("节日/圣诞", ["圣诞","雪橇","村庄","童话","节日","壁炉"]),
    ("夏日/海岛", ["夏日","夏夜","海岛","泻湖","沙滩","海边","椰枣","绿洲","脚印","海浪"]),
]

TAG_KEYWORDS = [
    "cinematic", "macro", "wildlife", "timelapse", "duel", "battle", "fire",
    "storm", "lightning", "ocean", "mountain", "forest", "city", "night",
    "sunset", "portrait", "samurai", "wuxia", "fantasy", "scifi",
    "campfire", "hummingbird", "tokyo", "kyoto", "rain", "snow", "sunrise",
    "cherry", "blossom", "sakura", "beach", "desert", "canyon", "aurora",
    "moon", "stars", "milkyway", "underwater", "dragon", "kite", "child",
    "running", "butterfly", "dandelion", "willow", "bamboo", "wolf", "eagle",
    "festival", "lantern", "snowfall", "fog", "mist",
]


def hashtags(prompt, max_tags=5):
    lower = prompt.lower()
    seen, out = set(), []
    for kw in TAG_KEYWORDS:
        if kw in lower and kw not in seen:
            out.append("#" + kw)
            seen.add(kw)
            if len(out) >= max_tags:
                break
    return out


def load_prompts():
    out = {}
    with open(CSV) as f:
        for r in csv.DictReader(f):
            if r.get("status") != "succeeded":
                continue
            tid = r.get("task_id", "").strip()
            if tid:
                out[tid] = r.get("prompt", "").strip().strip('"')
    return out


def dedup_videos():
    """base -> Path (prefer no -1/-2 suffix)."""
    by_base = {}
    for p in VIDEOS.glob("cgt-*.mp4"):
        m = re.match(r"^(cgt-\d+-[a-z0-9]+)(?:-\d)?$", p.stem)
        if not m:
            continue
        by_base.setdefault(m.group(1), []).append(p)
    out = {}
    for base, paths in by_base.items():
        primary = [p for p in paths if p.stem == base]
        out[base] = primary[0] if primary else min(paths, key=lambda x: x.stat().st_mtime)
    return out


def classify(prompt):
    for theme, kws in THEMES:
        for kw in kws:
            if kw in prompt:
                return theme
    return "未分类"


def main():
    prompts = load_prompts()
    videos = dedup_videos()
    print(f"succeeded: {len(prompts)}, unique videos: {len(videos)}")

    # build pool: base, prompt, path
    pool = []
    for base, path in videos.items():
        p = prompts.get(base)
        if not p:
            continue
        pool.append({
            "base": base,
            "path": path,
            "prompt": p,
            "chars": len(p),
            "mtime": path.stat().st_mtime,
        })

    # group by theme
    by_theme = {}
    for item in pool:
        t = classify(item["prompt"])
        by_theme.setdefault(t, []).append(item)

    # per theme: pick best (highest chars*2 + mtime_rank_score)
    picks = []
    for theme, items in by_theme.items():
        items.sort(key=lambda x: (x["chars"] * 2 + x["mtime"]), reverse=True)
        best = items[0]
        picks.append({
            "theme": theme,
            "base": best["base"],
            "path": str(best["path"]),
            "name": best["path"].name,
            "prompt": best["prompt"],
            "score": best["chars"] * 2 + best["mtime"],
        })
        print(f"  [{theme:8s}] pool={len(items):2d}  pick: {best['base']}  ({best['chars']} chars)  {best['prompt'][:50]}")

    picks.sort(key=lambda x: x["score"], reverse=True)

    # build queue skeleton
    out = []
    for p in picks:
        path = Path(p["path"])
        title = p["prompt"].split(",")[0].strip()[:60]
        desc = (p["prompt"] + " " + " ".join(hashtags(p["prompt"])))[:500]
        out.append({
            "path": p["path"],
            "name": p["name"],
            "date": "2026-08-31",
            "mtime": path.stat().st_mtime,
            "size": path.stat().st_size,
            "title": title,
            "description": desc,
            "hashtags": hashtags(p["prompt"]),
            "published": {},
            "_theme": p["theme"],
        })

    PICKS_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nwrote {len(out)} picks → {PICKS_FILE}")


if __name__ == "__main__":
    main()
