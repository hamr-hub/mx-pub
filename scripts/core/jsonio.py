"""Crash-safe JSON state I/O.

Every runtime state file in this project (publish_queue.json,
publish_state.json, collections_state.json, pacing_state.json) is rewritten
in place. A bare `write_text()` leaves a truncated file if the process dies
mid-write, and the next load then raises JSONDecodeError — losing the publish
queue outright. The tmp+replace idiom had already been copy-pasted into
several modules; this is the single implementation.

`replace()` is atomic on POSIX and Windows, so a concurrent reader sees either
the old file or the new one, never a half-written one.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def atomic_write_json(path: str | os.PathLike, payload: Any, *,
                      indent: int | None = 2, ensure_ascii: bool = False) -> None:
    """Serialise `payload` to `path` atomically.

    Writes a sibling temp file in the same directory (so replace() stays on one
    filesystem) and renames it into place.
    """
    path = Path(path)
    text = json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory),
                                    prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_safe(path: str | os.PathLike,
                   default: Optional[Any] = None) -> Any:
    """Load JSON, returning `default` on missing/corrupt/unexpected-shape files.

    State files are telemetry and bookkeeping, not a reason to fail a publish.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default
