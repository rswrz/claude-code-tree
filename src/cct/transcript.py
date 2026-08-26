"""Turning one ``*.jsonl`` transcript into a :class:`~cct.model.Session`.

Single-pass: a project directory holds hundreds of these, and the largest run to
hundreds of megabytes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from .model import Session

# Search-haystack budgets. Enough text to find a session by what was said in it,
# little enough that a full project stays in memory.
HAY_MSG = 300
HAY_USER = 2000
HAY_ASST = 1000

FIRST_MAX = 120

USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def block_text(content: object) -> str:
    """Flatten a message body to its plain text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return ""


def sid_of(path: str) -> str:
    name = os.path.basename(path)
    return name[:-len(".jsonl")] if name.endswith(".jsonl") else name


def parse_session(path: str, sid: str | None = None) -> Session:
    stat = os.stat(path)
    session = Session(
        sid=sid or sid_of(path),
        path=path,
        project_dir=os.path.dirname(path),
        mtime=stat.st_mtime,
    )

    relocated = ""
    frames: set[str] = set()
    has_turns = False
    have_ctx = False
    hay_user: list[str] = []
    hay_asst: list[str] = []
    user_budget = asst_budget = 0

    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")

            if kind == "custom-title":
                value = rec.get("customTitle")
                if isinstance(value, str) and value.strip():
                    session.custom = value.strip()
                continue
            if kind == "ai-title":
                value = rec.get("aiTitle")
                if isinstance(value, str) and value.strip():
                    session.ai = value.strip()
                continue
            if kind == "pr-link":
                try:
                    session.prs.add(int(rec.get("prNumber")))
                except (TypeError, ValueError):
                    pass
                continue
            if kind == "frame-link":
                url = rec.get("frameUrl")
                if isinstance(url, str) and url:
                    frames.add(url)
                continue
            if kind == "worktree-state":
                worktree = rec.get("worktreeSession") or {}
                if isinstance(worktree, dict):
                    name = worktree.get("worktreeName")
                    if isinstance(name, str) and name.strip():
                        session.worktree = name.strip()
                continue
            if kind == "relocated":
                cwd = rec.get("relocatedCwd")
                if isinstance(cwd, str) and cwd.strip():
                    relocated = os.path.basename(cwd.rstrip("/"))
                continue
            if kind not in ("user", "assistant"):
                continue

            has_turns = True
            if "uuid" in rec:
                session.uuids.append(rec["uuid"])
            branch = rec.get("gitBranch")
            if isinstance(branch, str) and branch:
                session.branch = branch
            cwd = rec.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                session.cwd = cwd.strip()
            stamp = rec.get("timestamp")
            if isinstance(stamp, str):
                when = parse_ts(stamp)
                if when is not None:
                    if session.ts_min is None or when < session.ts_min:
                        session.ts_min = when
                    if session.ts_max is None or when > session.ts_max:
                        session.ts_max = when
            if kind == "assistant" and not rec.get("isSidechain"):
                usage = (rec.get("message") or {}).get("usage")
                if isinstance(usage, dict):
                    # the last main-chain turn carries the live context size
                    session.ctx = sum(usage.get(k) or 0 for k in USAGE_KEYS)
                    have_ctx = True
            session.count += 1

            if kind == "user" and not rec.get("isMeta") and not rec.get("isSidechain"):
                body = " ".join(block_text((rec.get("message") or {}).get("content")).split())
                if body and not body.startswith("<"):
                    session.users += 1
                    if not session.first:
                        session.first = body[:FIRST_MAX]
                    if user_budget < HAY_USER:
                        hay_user.append(body[:HAY_MSG])
                        user_budget += len(body[:HAY_MSG]) + 1
            elif kind == "assistant" and asst_budget < HAY_ASST:
                body = " ".join(block_text((rec.get("message") or {}).get("content")).split())
                if body:
                    hay_asst.append(body[:HAY_MSG])
                    asst_budget += len(body[:HAY_MSG]) + 1

    session.frames = len(frames)
    session.worktree = session.worktree or relocated
    if not have_ctx:
        session.ctx = None
    session.stub = not has_turns
    session.hay = " ".join(hay_user + hay_asst)
    return session


def read_custom_title(path: str) -> str:
    """The last ``custom-title`` record wins; that is the name the session shows."""
    last = ""
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "custom-title":
                    title = rec.get("customTitle")
                    if isinstance(title, str):
                        last = title
    except OSError:
        return ""
    return last


def append_custom_title(path: str, sid: str, name: str) -> None:
    record = {"type": "custom-title", "customTitle": name, "sessionId": sid}
    with open(path, "a") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
