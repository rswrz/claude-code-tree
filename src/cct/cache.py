"""On-disk parse cache, one file per project directory.

A transcript is re-read only when its size or mtime moved. Nothing in here may
raise into the scanner: a broken or unwritable cache degrades to a slow scan.
"""

from __future__ import annotations

import json
import os

from . import paths
from .model import Session
from .transcript import parse_ts

CACHE_V = 1


def cache_path(encoded: str) -> str:
    return os.path.join(paths.CACHE_DIR, encoded + ".json")


def to_json(session: Session) -> dict:
    return {
        "cwd": session.cwd,
        "mtime": session.mtime,
        "uuids": session.uuids,
        "count": session.count,
        "users": session.users,
        "first": session.first,
        "custom": session.custom,
        "ai": session.ai,
        "branch": session.branch,
        "worktree": session.worktree,
        "prs": sorted(session.prs),
        "frames": session.frames,
        "ctx": session.ctx,
        "ts_min": session.ts_min.isoformat() if session.ts_min else None,
        "ts_max": session.ts_max.isoformat() if session.ts_max else None,
        "stub": session.stub,
        "hay": session.hay,
    }


def from_json(sid: str, path: str, data: dict) -> Session:
    return Session(
        sid=sid,
        path=path,
        project_dir=os.path.dirname(path),
        cwd=data.get("cwd") or "",
        mtime=float(data.get("mtime") or 0.0),
        uuids=list(data.get("uuids") or ()),
        count=int(data.get("count") or 0),
        users=int(data.get("users") or 0),
        first=data.get("first") or "",
        custom=data.get("custom") or "",
        ai=data.get("ai") or "",
        branch=data.get("branch") or "",
        worktree=data.get("worktree") or "",
        prs=set(data.get("prs") or ()),
        frames=int(data.get("frames") or 0),
        ctx=data.get("ctx"),
        ts_min=parse_ts(data["ts_min"]) if data.get("ts_min") else None,
        ts_max=parse_ts(data["ts_max"]) if data.get("ts_max") else None,
        stub=bool(data.get("stub")),
        hay=data.get("hay") or "",
    )


class Cache:
    """The parse results for one project directory, keyed by session id."""

    def __init__(self, encoded: str, enabled: bool = True) -> None:
        self.encoded = encoded
        self.enabled = enabled
        self.path = cache_path(encoded)
        self._old: dict = {}
        self._new: dict = {}
        self._hits = 0
        if enabled:
            self._old = self._read()

    def _read(self) -> dict:
        try:
            with open(self.path, errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def hits(self) -> int:
        return self._hits

    def get(self, sid: str, path: str, stat: os.stat_result) -> Session | None:
        if not self.enabled:
            return None
        entry = self._old.get(sid)
        if not isinstance(entry, dict):
            return None
        if (entry.get("v") != CACHE_V
                or entry.get("size") != stat.st_size
                or entry.get("mtime") != stat.st_mtime):
            return None
        try:
            session = from_json(sid, path, entry["data"])
        except (KeyError, TypeError, ValueError):
            return None
        self._new[sid] = entry
        self._hits += 1
        return session

    def put(self, sid: str, stat: os.stat_result, session: Session) -> None:
        if not self.enabled:
            return
        try:
            self._new[sid] = {
                "v": CACHE_V,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "data": to_json(session),
            }
        except (TypeError, ValueError):
            pass

    def save(self) -> None:
        """Replace the file with exactly the sessions seen this scan."""
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = "%s.%d.tmp" % (self.path, os.getpid())
            with open(tmp, "w") as fh:
                json.dump(self._new, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            pass
