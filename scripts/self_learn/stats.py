"""Self-learning stats store.

Records every publish attempt with rich context — platform, time-of-day, content
metadata, classification, action, duration — so we can later learn which
strategies work best when.

Persisted to disk as newline-delimited JSON (one event per line). Append-only
during the run; compactable on demand. Easy to grep, easy to stream into
pandas / sqlite / anything.

Schema (see Event dataclass):

    ts           float       epoch seconds
    platform     str
    video_id     str         opaque; usually queue title
    status       str         ok / partial / fail / blocked
    method       str         cdp / api / gui
    duration_s   float
    error        str         short error text
    cls          str         ErrorClass value
    action       str         Action value
    severity     int         classification severity
    hour         int         0..23 (derived from ts at write time)
    weekday      int         0..6 Mon..Sun (derived from ts at write time)
    content_hash str         sha1(title+description[:200]) for grouping
    title_len    int         length of title at attempt
    topics_n     int         number of topics attached
    attempt_n    int         1 for first try, 2 for retry, etc.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "publish_events.jsonl"
MAX_FILE_MB = 50          # rotate when above this


def _content_hash(*parts: Optional[str]) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


@dataclass
class Event:
    ts: float
    platform: str
    video_id: str
    status: str
    method: str
    duration_s: float
    error: str = ""
    cls: str = ""
    action: str = ""
    severity: int = 0
    hour: int = 0
    weekday: int = 0
    content_hash: str = ""
    title_len: int = 0
    topics_n: int = 0
    attempt_n: int = 1
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extras", None)
        if self.extras:
            d["extras"] = self.extras
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        valid = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in valid})

    @classmethod
    def make(cls, *, platform: str, video_id: str, status: str,
             method: str, duration_s: float, error: str = "",
             cls_name: str = "", action: str = "", severity: int = 0,
             title: str = "", description: str = "",
             topics: Optional[list[str]] = None,
             attempt_n: int = 1, extras: Optional[dict] = None) -> "Event":
        now = time.time()
        ts_struct = time.localtime(now)
        return cls(
            ts=now,
            platform=platform,
            video_id=video_id,
            status=status,
            method=method,
            duration_s=duration_s,
            error=(error or "")[:200],
            cls=cls_name,
            action=action,
            severity=severity,
            hour=ts_struct.tm_hour,
            weekday=ts_struct.tm_wday,
            content_hash=_content_hash(title, description[:200] if description else ""),
            title_len=len(title or ""),
            topics_n=len(topics or []),
            attempt_n=attempt_n,
            extras=extras or {},
        )


class StatsStore:
    """Append-only JSONL store with quick in-memory read APIs."""

    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: list[Event] = []
        self._cache_dirty = True

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._cache.append(event)
            self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                ts = time.strftime("%Y%m%d-%H%M%S")
                rotated = self.path.with_suffix(f".{ts}.jsonl")
                self.path.rename(rotated)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def all(self, *, refresh: bool = False) -> list[Event]:
        if self._cache_dirty or refresh or not self._cache:
            self._load_into_cache()
        return list(self._cache)

    def _load_into_cache(self) -> None:
        out: list[Event] = []
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            out.append(Event.from_dict(d))
                        except Exception:
                            continue
            except Exception:
                pass
        self._cache = out
        self._cache_dirty = False

    def iter_since(self, since_ts: float) -> Iterator[Event]:
        for ev in self.all():
            if ev.ts >= since_ts:
                yield ev

    def since(self, since_ts: float) -> list[Event]:
        return list(self.iter_since(since_ts))

    def for_platform(self, platform: str,
                     *, since_ts: Optional[float] = None) -> list[Event]:
        since_ts = since_ts or 0.0
        return [ev for ev in self.all() if ev.platform == platform
                and ev.ts >= since_ts]

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------

    def success_rate(self, platform: Optional[str] = None,
                     *, since_ts: Optional[float] = None,
                     window: Optional[int] = None) -> float:
        events = self.all()
        if since_ts is not None:
            events = [e for e in events if e.ts >= since_ts]
        if platform is not None:
            events = [e for e in events if e.platform == platform]
        if window is not None and len(events) > window:
            events = events[-window:]
        if not events:
            return 1.0
        ok = sum(1 for e in events if e.status == "ok")
        return ok / len(events)

    def hour_success_matrix(self, platform: str) -> dict:
        """Return {hour: success_rate} over the full history for that platform."""
        bucket: dict[int, list[int]] = defaultdict(list)
        for ev in self.all():
            if ev.platform != platform:
                continue
            bucket[ev.hour].append(1 if ev.status == "ok" else 0)
        return {h: (sum(v) / len(v) if v else 0.0) for h, v in bucket.items()}

    def best_hours(self, platform: str, *, min_samples: int = 3,
                   top_k: int = 3) -> list[tuple[int, float]]:
        m = self.hour_success_matrix(platform)
        return sorted(
            ((h, r) for h, r in m.items() if self._hour_count(platform, h) >= min_samples),
            key=lambda x: x[1], reverse=True,
        )[:top_k]

    def worst_hours(self, platform: str, *, min_samples: int = 3,
                    top_k: int = 3) -> list[tuple[int, float]]:
        m = self.hour_success_matrix(platform)
        return sorted(
            ((h, r) for h, r in m.items() if self._hour_count(platform, h) >= min_samples),
            key=lambda x: x[1],
        )[:top_k]

    def _hour_count(self, platform: str, hour: int) -> int:
        return sum(1 for e in self.all()
                   if e.platform == platform and e.hour == hour)

    def error_label_counts(self, platform: Optional[str] = None) -> Counter:
        c: Counter = Counter()
        for ev in self.all():
            if platform is not None and ev.platform != platform:
                continue
            if ev.cls:
                c[(ev.cls, ev.action)] += 1
        return c

    def recent_failures(self, platform: str, *, window: int = 10) -> list[Event]:
        events = [e for e in self.all() if e.platform == platform]
        return events[-window:][::-1]

    def summary(self) -> dict:
        events = self.all()
        by_platform: dict[str, dict] = {}
        for plat in {e.platform for e in events}:
            ps = [e for e in events if e.platform == plat]
            ok = sum(1 for e in ps if e.status == "ok")
            by_platform[plat] = {
                "total": len(ps),
                "ok": ok,
                "success_rate": ok / len(ps) if ps else 0.0,
                "avg_duration_s": round(
                    sum(e.duration_s for e in ps) / len(ps), 1) if ps else 0.0,
            }
        # error_label_counts() is keyed by (class, action) tuples, which
        # json.dumps cannot use as dict keys — `mxpub stats` crashed on any
        # history containing a failure. Flatten to "class/action" strings.
        labels: dict[str, int] = {}
        for (cls, action), n in self.error_label_counts().items():
            labels[f"{cls}/{action}"] = labels.get(f"{cls}/{action}", 0) + n
        return {
            "total_events": len(events),
            "by_platform": by_platform,
            "best_hours": {p: self.best_hours(p) for p in by_platform},
            "worst_hours": {p: self.worst_hours(p) for p in by_platform},
            "error_labels": labels,
        }
