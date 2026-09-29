"""Platform & content provider registry.

Goals:
- Add a new platform (e.g. bilibili, youtube shorts, tiktok, weibo) by
  registering a module + a tiny metadata dict — no changes to batch_publish.py.
- Allow multiple content generators to coexist; orchestrator picks the highest
  priority one that can handle the request.
- All registrations are validated at register-time so a typo'd entry fails
  immediately instead of at publish time.

Usage (in a platform plugin module):

    from extension.registry import register_platform, PlatformSpec

    register_platform("bilibili", PlatformSpec(
        name="bilibili",
        display_name="Bilibili",
        url_match=lambda u: "member.bilibili.com" in u,
        publish_url="https://member.bilibili.com/v2#/upload",
        module="platforms.bilibili",
        title_max=80,
        desc_max=2000,
        topics_max=10,
        short_title_required=False,
        force_skip=False,
    ))

The orchestrator auto-discovers this on startup via load_extensions().
"""
from __future__ import annotations

import importlib
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_log = logging.getLogger("mx-pub.extension")


@dataclass
class PlatformSpec:
    name: str
    display_name: str
    url_match: Callable[[str], bool]
    publish_url: str
    module: str               # dotted path to the platform module
    title_max: int = 30
    desc_max: int = 1000
    topics_max: int = 3
    short_title_required: bool = False
    short_title_min: int = 6
    short_title_max: int = 14
    force_skip: bool = False
    force_skip_reason: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class ContentProviderSpec:
    name: str
    module: str               # dotted path with generate_content(video, platform)
    priority: int = 50        # higher = preferred
    can_handle: Callable[..., bool] = lambda **kw: True


class _Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._platforms: dict[str, PlatformSpec] = {}
        self._providers: list[ContentProviderSpec] = []
        self._default_platforms: list[str] = [
            "douyin", "kuaishou", "weixin", "xhs",
        ]

    # ------------------------------------------------------------------
    # Platforms
    # ------------------------------------------------------------------

    def register_platform(self, spec: PlatformSpec) -> None:
        if not spec.name or not spec.module:
            raise ValueError("PlatformSpec requires name and module")
        with self._lock:
            if spec.name in self._platforms:
                _log.warning("platform %r already registered; overwriting",
                             spec.name)
            self._platforms[spec.name] = spec

    def unregister_platform(self, name: str) -> None:
        with self._lock:
            self._platforms.pop(name, None)

    def get_platform(self, name: str) -> Optional[PlatformSpec]:
        return self._platforms.get(name)

    def list_platforms(self) -> list[PlatformSpec]:
        return list(self._platforms.values())

    def active_platforms(self, *, extra_skip: Optional[set[str]] = None) -> list[str]:
        skip = set(extra_skip or ())
        out = []
        for spec in self._platforms.values():
            if spec.force_skip:
                skip.add(spec.name)
                continue
            if spec.name in skip:
                continue
            out.append(spec.name)
        return out or list(self._default_platforms)

    # ------------------------------------------------------------------
    # Content providers
    # ------------------------------------------------------------------

    def register_content_provider(self, spec: ContentProviderSpec) -> None:
        with self._lock:
            self._providers.append(spec)
            self._providers.sort(key=lambda p: -p.priority)

    def list_content_providers(self) -> list[ContentProviderSpec]:
        return list(self._providers)

    def resolve_content_provider(self, **kwargs) -> Optional[ContentProviderSpec]:
        for p in self._providers:
            try:
                if p.can_handle(**kwargs):
                    return p
            except Exception as e:
                _log.warning("can_handle for %s failed: %s", p.name, e)
        return None


# Module-level singleton.
Registry = _Registry()


# ---------------------------------------------------------------------------
# Convenience decorators so plugin code reads naturally
# ---------------------------------------------------------------------------

def register_platform(name: str = "", **kwargs):
    """Decorator / function form:

        @register_platform("bilibili")
        class BilibiliSpec: ...

    or

        register_platform(PlatformSpec(name="bilibili", ...))
    """
    def wrap(spec_or_cls):
        if isinstance(spec_or_cls, PlatformSpec):
            Registry.register_platform(spec_or_cls)
            return spec_or_cls
        # Called with class; build a PlatformSpec from kwargs on the class.
        spec = PlatformSpec(name=name or spec_or_cls.__name__.lower(), **kwargs)
        Registry.register_platform(spec)
        return spec_or_cls
    return wrap


def register_content_provider(name: str = "", priority: int = 50,
                                module: str = "", **kwargs):
    spec = ContentProviderSpec(
        name=name, module=module, priority=priority, **kwargs)
    Registry.register_content_provider(spec)
    return spec


# ---------------------------------------------------------------------------
# Plugin loader — picks up .py files in extension/
# ---------------------------------------------------------------------------

def load_extensions(extension_dir: Optional[Path] = None) -> list[str]:
    """Import every .py in extension_dir so module-level @register_* calls fire.

    Returns the names of modules successfully loaded.
    """
    if extension_dir is None:
        extension_dir = Path(__file__).resolve().parent.parent / "extension"
    extension_dir = Path(extension_dir)
    if not extension_dir.exists():
        return []
    loaded = []
    import sys
    for f in sorted(extension_dir.glob("*.py")):
        if f.name.startswith("_") or f.name == "registry.py":
            continue
        modname = f"mx_extensions.{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(modname, f)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            loaded.append(modname)
        except Exception as e:
            _log.warning("extension %s failed to load: %s", f.stem, e)
    return loaded


# ---------------------------------------------------------------------------
# Self-registration of the built-in platforms so the rest of the code can
# rely on Registry.get_platform() always returning something for them.
# ---------------------------------------------------------------------------

def register_builtin_platforms() -> None:
    """Register the four platforms shipped with mx-pub. Idempotent."""
    if Registry.get_platform("douyin"):
        return
    for spec in [
        PlatformSpec(
            name="douyin", display_name="抖音",
            url_match=lambda u: "creator.douyin.com" in u,
            publish_url="https://creator.douyin.com/creator-micro/content/publish",
            module="platforms.douyin",
            title_max=30, desc_max=900, topics_max=3,
            short_title_required=False,
        ),
        PlatformSpec(
            name="kuaishou", display_name="快手",
            url_match=lambda u: "cp.kuaishou.com/article/publish" in u,
            publish_url="https://cp.kuaishou.com/article/publish/video",
            module="platforms.kuaishou",
            title_max=30, desc_max=500, topics_max=4,
            short_title_required=False,
        ),
        PlatformSpec(
            name="weixin", display_name="视频号",
            url_match=lambda u: "channels.weixin" in u,
            publish_url="https://channels.weixin.qq.com/platform/post/create",
            module="platforms.weixin",
            title_max=14, desc_max=1000, topics_max=3,
            short_title_required=True,
            short_title_min=6, short_title_max=14,
        ),
        PlatformSpec(
            name="xhs", display_name="小红书",
            url_match=lambda u: "creator.xiaohongshu.com" in u and "publish/publish" in u,
            publish_url="https://creator.xiaohongshu.com/publish/publish?from=menu&target=video",
            module="platforms.xhs",
            title_max=20, desc_max=1000, topics_max=10,
            short_title_required=False,
            force_skip=True,
            force_skip_reason=(
                "XHS auto-publish carries account-ban / AI-detection risk; "
                "enable explicitly via ONLY_PLATFORMS or per-task whitelist"),
        ),
    ]:
        Registry.register_platform(spec)
