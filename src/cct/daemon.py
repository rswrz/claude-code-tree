"""What the Claude daemon knows: the agents view, the worker roster, job state.

Every entry point fails soft: the daemon may be down, mid-write, or a different
version, and a tree missing a badge still beats no tree.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from . import paths
from .model import Agent

AGENTS_TIMEOUT = 5.0
PR_TRAILING = re.compile(r"(\d+)\s*$")
PR_IN_URL = re.compile(r"/pull/(\d+)")


def read_json(path: str) -> dict:
    try:
        with open(path, errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _epoch_seconds(value: object) -> float:
    try:
        stamp = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return stamp / 1000.0 if stamp > 1e11 else stamp


def list_agents(cwd: str | None = None, all_: bool = True) -> list[Agent]:
    cmd = ["claude", "agents", "--json"]
    if all_:
        cmd.append("--all")
    if cwd:
        cmd += ["--cwd", cwd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=AGENTS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    agents = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        agents.append(Agent(
            sid=sid,
            kind=row.get("kind") or "",
            state=row.get("state") or "",
            status=row.get("status") or "",
            live=row.get("pid") is not None,
            name=(row.get("name") or "").strip(),
            started_at=_epoch_seconds(row.get("startedAt")),
            cwd=row.get("cwd") or "",
        ))
    return agents


def roster_workers() -> dict:
    workers = read_json(paths.ROSTER).get("workers")
    return workers if isinstance(workers, dict) else {}


def live_sids() -> set[str]:
    out = set()
    for worker in roster_workers().values():
        if isinstance(worker, dict) and isinstance(worker.get("sessionId"), str):
            out.add(worker["sessionId"])
    return out


def is_live(sid: str) -> bool:
    """True while the daemon holds a worker for this session."""
    for worker in roster_workers().values():
        if isinstance(worker, dict) and worker.get("sessionId") == sid:
            return True
    return False


def roster_fork_parents() -> dict[str, str]:
    """Sessions the daemon forked, mapped to the transcript they were forked from."""
    out: dict[str, str] = {}
    for worker in roster_workers().values():
        if not isinstance(worker, dict):
            continue
        sid = worker.get("sessionId")
        launch = ((worker.get("dispatch") or {}).get("launch")) or {}
        source = launch.get("sessionId")
        if not (isinstance(sid, str) and isinstance(source, str) and launch.get("fork")):
            continue
        base = os.path.basename(source)
        if base.endswith(".jsonl"):
            base = base[:-len(".jsonl")]
        if base:
            out[sid] = base
    return out


def job_ids() -> set[str]:
    """The eight-character job directory names, so a scan can skip absent jobs."""
    try:
        return {name for name in os.listdir(paths.JOBS_DIR)}
    except OSError:
        return set()


def job_state(sid: str) -> dict:
    return read_json(os.path.join(paths.job_dir(sid), "state.json"))


def job_prs(job: dict) -> set[int]:
    """PR numbers the job linked, from the id, then from the href."""
    out: set[int] = set()
    for child in (job.get("children") or []):
        if not isinstance(child, dict) or child.get("kind") != "pr":
            continue
        ident = child.get("id")
        if isinstance(ident, int):
            out.add(ident)
            continue
        match = PR_TRAILING.search(str(ident or ""))
        if not match:
            match = PR_IN_URL.search(str(child.get("href") or ""))
        if match:
            out.add(int(match.group(1)))
    return out


def job_timeline(sid: str, n: int = 5) -> list[str]:
    """The last ``n`` timeline entries, each collapsed to one line."""
    path = os.path.join(paths.job_dir(sid), "timeline.jsonl")
    try:
        with open(path, errors="replace") as fh:
            lines = [line.strip() for line in fh if line.strip()][-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(" ".join(json.dumps(json.loads(line), ensure_ascii=False).split()))
        except ValueError:
            out.append(line)
    return out
