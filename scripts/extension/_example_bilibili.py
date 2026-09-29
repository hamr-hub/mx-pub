"""Example extension: register a hypothetical Bilibili publisher.

This is a TEMPLATE / REFERENCE for adding new platforms without modifying
batch_publish.py or any other core file. Drop a real platform module under
scripts/platforms/, replace the spec below with the real values, and the rest
of mx-pub will pick it up at startup via load_extensions().

Naming convention: any *.py file in this directory except those starting with
_ or named registry.py is auto-loaded.
"""
from extension.registry import register_platform, PlatformSpec


register_platform(PlatformSpec(
    name="bilibili",
    display_name="哔哩哔哩",
    url_match=lambda u: "member.bilibili.com" in u,
    publish_url="https://member.bilibili.com/v2#/upload",
    module="platforms.bilibili",
    title_max=80,
    desc_max=2000,
    topics_max=10,
    short_title_required=False,
    force_skip=False,
    extras={
        "supports_subtitle": True,
        "supports_danmaku": True,
    },
))
