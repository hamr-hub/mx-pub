"""Extension hook system.

The hook system is the spine of the publisher's extensibility story:
- New AI providers hook into content generation
- New safety / compliance rules hook into pre-publish
- New platforms just register a module + a few hooks
- Observability hooks (stats, insights, alerting) plug in here without
  touching the core publish path

Hook names follow a verb-object lifecycle:

    platform:register              fired once at startup; args: PlatformRegistry
    platform:before_setup          args: page, platform
    platform:after_setup           args: page, platform, report
    video:before_publish           args: video, platform
    video:after_publish            args: video, platform, result
    video:on_error                 args: video, platform, classification
    content:before_generate        args: video, platform, prompt
    content:after_generate         args: video, platform, content
    batch:before                   args: queue
    batch:after                    args: queue, summary
    insights:report                args: insights (text)

Each hook is a callable. Hooks are registered via `register(name, fn)` and
removed via `unregister(name, fn)`. Hooks run synchronously in registration
order; failures in one hook do NOT abort the chain (logged + skipped).

Usage:

    from core.hooks import Hooks
    Hooks.register("video:after_publish", my_telemetry)
    Hooks.emit("video:after_publish", video=v, platform="douyin", result=r)

A typical integration point in a platform module:

    Hooks.emit("video:before_publish", video=video, platform=self.name)
    ... do publish ...
    Hooks.emit("video:after_publish", video=video, platform=self.name,
               result=result)
"""
from __future__ import annotations

import logging
import threading
import traceback
from collections import defaultdict
from typing import Any, Callable

_log = logging.getLogger("mx-pub.hooks")

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class _HookRegistry:
    def __init__(self):
        self._hooks: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def register(self, name: str, fn: Callable) -> Callable:
        """Register fn under name. Returns fn so it can be used as a decorator."""
        with self._lock:
            if fn not in self._hooks[name]:
                self._hooks[name].append(fn)
        return fn

    def unregister(self, name: str, fn: Callable) -> None:
        with self._lock:
            if fn in self._hooks[name]:
                self._hooks[name].remove(fn)

    def clear(self, name: str | None = None) -> None:
        """Clear a single hook or all hooks (for tests)."""
        with self._lock:
            if name is None:
                self._hooks.clear()
            else:
                self._hooks.pop(name, None)

    def listeners(self, name: str) -> list[Callable]:
        with self._lock:
            return list(self._hooks.get(name, ()))

    def emit(self, name: str, **kwargs: Any) -> list[Any]:
        """Run all hooks under name with kwargs. Returns list of return values.

        Errors are logged and swallowed — a failing telemetry hook must never
        break a publish.
        """
        results = []
        for fn in self.listeners(name):
            try:
                results.append(fn(**kwargs))
            except Exception as e:
                _log.warning("hook %s failed: %s\n%s",
                             name, e, traceback.format_exc(limit=2))
        return results


# Module-level singleton — kept simple so hooks can be registered from anywhere.
Hooks = _HookRegistry()


# ---------------------------------------------------------------------------
# Built-in: NOOP fallback + a couple of "always useful" decorators
# ---------------------------------------------------------------------------

def emit_if(condition: Callable[..., bool]):
    """Decorator: emit only when the predicate returns True.

    Example:
        @emit_if(lambda platform, **kw: platform == "douyin")
        def my_douyin_hook(...): ...
    """
    def wrap(fn):
        def inner(**kwargs):
            try:
                if condition(**kwargs):
                    return fn(**kwargs)
            except Exception as e:
                _log.warning("hook %s predicate failed: %s", fn.__name__, e)
            return None
        inner.__wrapped__ = fn
        Hooks.register(_hook_name(fn), inner)
        return fn
    return wrap


def _hook_name(fn: Callable) -> str:
    """Default hook name: '<module>:<funcname>' or just funcname."""
    mod = getattr(fn, "__module__", "") or ""
    return f"{mod.replace('.', ':')}:{fn.__name__}" if mod else fn.__name__


# ---------------------------------------------------------------------------
# Discovery helpers — load extra hook modules from a directory
# ---------------------------------------------------------------------------

def load_plugin_modules(directory: str) -> list[str]:
    """Import every *.py file in `directory` so its module-level
    @Hooks.register calls take effect. Returns the names successfully loaded.

    Designed for the extension/ directory.
    """
    import importlib
    import importlib.util
    import sys
    from pathlib import Path

    p = Path(directory)
    if not p.exists() or not p.is_dir():
        return []
    loaded = []
    for f in sorted(p.glob("*.py")):
        if f.name.startswith("_"):
            continue
        modname = f"mx_plugins.{f.stem}"
        spec = importlib.util.spec_from_file_location(modname, f)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        try:
            spec.loader.exec_module(mod)
            loaded.append(modname)
        except Exception as e:
            _log.warning("plugin %s failed to load: %s", modname, e)
    return loaded
