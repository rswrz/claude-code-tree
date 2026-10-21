"""Session actions: rename, delete, prune stubs, links, resume, fork, new and dispatch.

Curses owns the terminal, so nothing here prints. Every action either returns a
plan for the UI to confirm, or a report for the UI to show in a modal.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from cct import daemon, paths
from cct.layout import fmt_age
from cct.model import Session, Snapshot

CLAUDE = "claude"
STEP_TIMEOUT = 60.0

# A stub touched this recently may be an interactive session still waiting for
# its first turn, so pruning leaves it alone.
FRESH_SECS = 900


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """One `claude` invocation and how it went."""

    label: str
    ok: bool
    code: int
    output: str = ""

    def lines(self) -> list[str]:
        head = f"{self.label}: ok" if self.ok else f"{self.label}: failed (exit {self.code})"
        body = [f"  {ln}" for ln in self.output.splitlines() if ln.strip()]
        return [head, *body]


@dataclass
class DeletePlan:
    sid: str
    title: str
    cwd: str
    daemon_known: bool = False
    job_state: str = ""
    job_worktree: str = ""
    paths: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"Session: {self.title}"]
        if self.title != self.sid:
            out.append(f"UUID:    {self.sid}")
        if self.daemon_known:
            out.append(
                f"Background session, state: {self.job_state}"
                if self.job_state
                else "Background session"
            )
            out.append("claude rm also deletes the session's worktree")
            if self.job_worktree:
                out.append(f"  - {self.job_worktree}")
        out.append("Files to remove:")
        out.extend(f"  - {p}" for p in self.paths)
        if not self.paths:
            out.append("  (none)")
        return out


@dataclass
class DeleteReport:
    sid: str
    steps: list[Step] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    agents_view: str = ""

    def lines(self) -> list[str]:
        out: list[str] = []
        for step in self.steps:
            out.extend(step.lines())
        out.extend(f"removed {p}" for p in self.removed)
        out.extend(f"could not remove {p}" for p in self.failed)
        if self.agents_view:
            out.append(f"agents view: {self.agents_view}")
        out.append("deleted." if not self.failed else "deleted with errors.")
        return out


@dataclass
class StubRow:
    sid: str
    title: str
    age: str
    job: str = ""
    fresh: bool = False
    live: bool = False
    mtime: float = 0.0
    paths: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return self.fresh or self.live

    def line(self) -> str:
        note = ""
        if self.fresh:
            note = "  just started, will be skipped"
        elif self.live:
            note = "  live in daemon, will be skipped"
        return f"{self.sid[:8]}  {(self.title or '—')[:48]:<48}  {self.age:<9} {self.job}{note}"


@dataclass
class PruneReport:
    steps: list[Step] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = list(self.skipped)
        for step in self.steps:
            out.extend(step.lines())
        out.append(f"pruned {len(self.pruned)}.")
        return out


@dataclass
class Link:
    url: str
    label: str


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _run(args: list[str], label: str = "", cwd: str | None = None) -> Step:
    label = label or " ".join([CLAUDE, *args])
    try:
        proc = subprocess.run(
            [CLAUDE, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=STEP_TIMEOUT,
            cwd=cwd or None,
        )
    except FileNotFoundError:
        return Step(label, False, 127, f"{CLAUDE}: not found")
    except subprocess.TimeoutExpired:
        return Step(label, False, 124, "timed out")
    except OSError as exc:
        return Step(label, False, 1, str(exc))
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return Step(label, proc.returncode == 0, proc.returncode, out)


def _records(path: str):
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _encoded_of(session: Session) -> str:
    if session.project_dir:
        return paths.encoded_of(session.project_dir)
    return paths.encode_dir(session.cwd) if session.cwd else ""


def _session_paths(session: Session) -> list[str]:
    return list(paths.session_paths(session.sid, session.project_dir, _encoded_of(session)))


def _remove(path: str) -> bool:
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        return True
    except OSError:
        return False


def _daemon_known(session: Session, snap: Snapshot) -> tuple[bool, dict]:
    job = snap.jobs.get(session.sid) or {}
    if job:
        return True, job
    agent = snap.agents.get(session.sid)
    return bool(agent and agent.live and agent.kind == "background"), job


def _claude_rm(sid: str) -> list[Step]:
    """`claude rm`; a running session has to be stopped before it will go."""
    short = sid[:8]
    step = _run(["rm", short], f"claude rm {short}")
    if step.ok:
        return [step]
    return [step, _run(["stop", short], f"claude stop {short}"), _run(["rm", short], f"claude rm {short}")]


# --------------------------------------------------------------------------- #
# rename
# --------------------------------------------------------------------------- #


def current_title(session: Session) -> str:
    """The name a rename starts from: the session's own custom title, or ''."""
    return session.custom


def rename(session: Session, new_name: str) -> str | None:
    """Append a `custom-title` record. Returns an error message, or None."""
    name = new_name.strip()
    if not name:
        return "empty name"
    if not session.path or not os.path.isfile(session.path):
        return f"session file not found: {session.path or session.sid}"
    record = {"type": "custom-title", "customTitle": name, "sessionId": session.sid}
    try:
        with open(session.path, "a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        return str(exc)
    return None


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def delete(session: Session, snap: Snapshot) -> DeletePlan:
    known, job = _daemon_known(session, snap)
    worktree = job.get("worktreePath")
    return DeletePlan(
        sid=session.sid,
        title=session.custom or session.title,
        cwd=session.cwd,
        daemon_known=known,
        job_state=str(job.get("state") or ""),
        job_worktree=worktree if isinstance(worktree, str) else "",
        paths=[p for p in _session_paths(session) if os.path.lexists(p)],
    )


def perform_delete(plan: DeletePlan) -> DeleteReport:
    report = DeleteReport(sid=plan.sid)
    if plan.daemon_known:
        report.steps.extend(_claude_rm(plan.sid))
    for path in plan.paths:
        (report.removed if _remove(path) else report.failed).append(path)
    if plan.daemon_known:
        report.agents_view = agents_view(plan.sid, plan.cwd)
    return report


def agents_view(sid: str, cwd: str) -> str:
    """What `claude agents` says about a session after a delete."""
    for agent in daemon.list_agents(cwd or None):
        if agent.sid == sid:
            return f"still listed ({agent.state or agent.status or '?'})"
    return "removed"


# --------------------------------------------------------------------------- #
# prune stubs
# --------------------------------------------------------------------------- #


def prune_stubs(snap: Snapshot, now: float | None = None) -> list[StubRow]:
    """Transcripts with no turns — they cannot be resumed, so they are litter."""
    now = time.time() if now is None else now
    rows: list[StubRow] = []
    for session in snap.sessions.values():
        if not session.stub or session.ghost:
            continue
        job = snap.jobs.get(session.sid) or {}
        agent = snap.agents.get(session.sid)
        rows.append(
            StubRow(
                sid=session.sid,
                title=session.custom or session.ai,
                age=fmt_age(session.mtime, now),
                job=("job:" + (str(job.get("state") or "") or "?")) if job else "",
                fresh=(now - session.mtime) < FRESH_SECS,
                live=bool(agent and agent.live),
                mtime=session.mtime,
                paths=_session_paths(session),
            )
        )
    rows.sort(key=lambda r: -r.mtime)
    return rows


def perform_prune(rows: list[StubRow]) -> PruneReport:
    report = PruneReport()
    for row in rows:
        if row.fresh:
            report.skipped.append(f"{row.sid[:8]}: touched in the last 15m, skipped")
            continue
        if row.live:
            report.skipped.append(f"{row.sid[:8]}: live in daemon, skipped")
            continue
        if row.job:
            report.steps.extend(_claude_rm(row.sid))
        for path in row.paths:
            _remove(path)
        report.pruned.append(row.sid)
    return report


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def links(session: Session, snap: Snapshot) -> list[Link]:
    seen: set[str] = set()
    out: list[Link] = []
    for obj in _records(session.path):
        kind = obj.get("type")
        if kind == "pr-link":
            url = obj.get("prUrl") or ""
            if url and url not in seen:
                seen.add(url)
                out.append(Link(url, f"#{obj.get('prNumber')}  {obj.get('prRepository') or ''}".rstrip()))
        elif kind == "frame-link":
            url = obj.get("frameUrl") or ""
            if url and url not in seen:
                seen.add(url)
                out.append(Link(url, f"⧉ {obj.get('title') or obj.get('path') or ''}".rstrip()))
    for child in (snap.jobs.get(session.sid) or {}).get("children") or []:
        if not isinstance(child, dict) or child.get("kind") != "pr":
            continue
        url = child.get("href") or ""
        if url and url not in seen:
            seen.add(url)
            out.append(Link(url, f"#{child.get('id')}"))
    for link in out:
        link.label = " ".join(link.label.split())
    return out


def open_link(url: str) -> str | None:
    """Hand a URL to the desktop. Returns an error message, or None."""
    try:
        proc = subprocess.run(["open", url], capture_output=True, text=True, timeout=STEP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or f"open: exit {proc.returncode}").strip()
    return None


# --------------------------------------------------------------------------- #
# resume / fork / new / dispatch
# --------------------------------------------------------------------------- #


def is_live(session: Session, snap: Snapshot) -> bool:
    agent = snap.agents.get(session.sid)
    return bool(agent and agent.live and agent.kind == "background")


def is_live_interactive(session: Session, snap: Snapshot) -> bool:
    """An interactive session with a pid: another terminal already has it open."""
    agent = snap.agents.get(session.sid)
    return bool(agent and agent.live and agent.kind != "background")


def resume_command(session: Session, live: bool = False) -> list[str]:
    if live:
        return [CLAUDE, "attach", session.sid[:8]]
    return [CLAUDE, "--resume", session.sid]


def fork_command(session: Session) -> list[str]:
    """Forking always goes through --resume; attach cannot fork."""
    return [CLAUDE, "--resume", session.sid, "--fork-session"]


def new_command(name: str = "") -> list[str]:
    """A session that does not exist yet, named up front when asked for."""
    name = name.strip()
    return [CLAUDE, "--name", name] if name else [CLAUDE]


def dispatch(prompt: str, cwd: str = "") -> Step:
    """Queue a background agent. `--bg` returns as soon as the job exists, so
    the terminal is never handed over."""
    return _run(["--bg", prompt], "claude --bg", cwd=cwd)


def work_dir(session: Session, snap: Snapshot, fallback: str = "") -> str:
    """Where a resumed session should start. Its own cwd wins.

    A worktree session starts in its repo root: Claude Code does not trust a
    worktree directory, and a resume from the root still finds the transcript.
    """
    project = snap.project_of(session.sid)
    for candidate in (session.origin, paths.repo_of(session.cwd),
                      paths.repo_of(project.cwd) if project else "", fallback):
        if candidate and os.path.isdir(candidate):
            return candidate
    return fallback or os.getcwd()


def new_dir(session: Session | None, snap: Snapshot, scope_dir: str, all_projects: bool) -> str:
    """Where a new session starts: the scope directory, or — browsing every
    project — the directory of the selected session."""
    if all_projects and session is not None:
        return work_dir(session, snap, scope_dir)
    return scope_dir
