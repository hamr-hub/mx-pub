"""Versioned mmx_h3 rerun renders must be visible to the curation gates.

`daily_clips()` globs `video_*.mp4` / `pro_video_*.mp4` and `read_daily_topics()`
reads exactly `all_providers.csv` / `prompts.csv`. The mmx_h3 pipeline also emits
`v2_video_NN.mp4` … `v4_video_NN.mp4` tracked by `v2_prompts.csv` … — none of
which matched, so `clip_index()` returned None for them and
`build_themed_queue.py` rejected every one of them with
"metadata not found in csv/series.json".

The fail-closed gates are only meaningful if they can SEE the clip: an
invisible clip is a permanently unpublishable one, which is the same failure
mode as a silent duplicate.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from curation import daily_clips, read_daily_topics  # noqa: E402

MANIFEST = ("provider,kind,index,model,duration_s,ratio,task_id,status,seconds,"
            "file_path,prompt\n")


def _day(tmp_path: Path, day: str, renders: list[str], manifests: dict) -> Path:
    """Lay out <tmp>/<day>/mmx_h3 with the given renders and manifests."""
    batch = tmp_path / day / "mmx_h3"
    batch.mkdir(parents=True)
    for name in renders:
        (batch / name).write_bytes(b"mp4")
    for manifest, rows in manifests.items():
        (batch / manifest).write_text(MANIFEST + rows, encoding="utf-8")
    return tmp_path / day


def test_versioned_renders_discovered_with_their_topic(tmp_path):
    day = _day(
        tmp_path, "20261015",
        renders=["v3_video_01.mp4", "v4_video_02.mp4"],
        manifests={
            "v3_prompts.csv": 'minimax-h3,video,01,MiniMax-H3,10,16:9,1,ok,10,'
                              '20261015/mmx_h3/v3_video_01.mp4,drowned subway junction\n',
            "v4_prompts.csv": 'minimax-h3,video,02,MiniMax-H3,10,16:9,2,ok,10,'
                              '20261015/mmx_h3/v4_video_02.mp4,jaipur stepwell paper folding\n',
        },
    )

    topics = read_daily_topics(day)
    assert topics["v3_video_01.mp4"] == "drowned subway junction"
    assert topics["v4_video_02.mp4"] == "jaipur stepwell paper folding"

    found = {c.path.name: c for c in daily_clips(tmp_path)}
    assert set(found) == {"v3_video_01.mp4", "v4_video_02.mp4"}
    for clip in found.values():
        assert clip.topic, f"{clip.name} discovered without topic metadata"
        assert clip.day == "20261015"


def test_legacy_and_pro_layouts_still_resolve(tmp_path):
    """The pre-existing layouts must not regress."""
    day = tmp_path / "20261014"
    batch = day / "mmx_h3"
    batch.mkdir(parents=True)
    for name in ("video_1.mp4", "pro_video_01.mp4"):
        (batch / name).write_bytes(b"mp4")
    (batch / "prompts.csv").write_text(
        MANIFEST
        + 'minimax-h3,video,01,MiniMax-H3,10,16:9,1,ok,10,'
          '20261014/mmx_h3/pro_video_01.mp4,pro prompt\n',
        encoding="utf-8",
    )
    (day / "all_providers.csv").write_text(
        "provider,kind,index,topic_seed,prompt,file_path,status\n"
        "minimax,video,1,chai foam,chai foam,video_1.mp4,ok\n",
        encoding="utf-8",
    )

    topics = read_daily_topics(day)
    assert topics["video_1.mp4"] == "chai foam"
    assert topics["pro_video_01.mp4"] == "pro prompt"
    assert {c.path.name for c in daily_clips(tmp_path)} == {
        "video_1.mp4", "pro_video_01.mp4"}


def test_appledouble_manifests_are_skipped(tmp_path):
    """`._v3_prompts.csv` is a binary AppleDouble stub, not CSV."""
    day = _day(tmp_path, "20261015", renders=["v3_video_01.mp4"],
               manifests={"v3_prompts.csv":
                          'minimax-h3,video,01,MiniMax-H3,10,16:9,1,ok,10,'
                          '20261015/mmx_h3/v3_video_01.mp4,aurora bee macro\n'})
    (day / "mmx_h3" / "._v3_prompts.csv").write_bytes(b"\x00\xd0\xff\x0bq\x00")

    topics = read_daily_topics(day)
    assert topics["v3_video_01.mp4"] == "aurora bee macro"
