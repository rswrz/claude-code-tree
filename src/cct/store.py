"""The scanner thread and the snapshots it publishes.

The UI never touches the disk: it reads :meth:`Loader.snapshot`, polls
:meth:`Loader.dirty` twice a second, and asks for a reload when that says yes.
"""

from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass

from . import cache as cache_mod
from . import daemon, gitinfo, paths, transcript, tree
from .model import Agent, Project, Session, Snapshot, Sort

# Below this many transcripts a change poll can afford to stat the files themselves,
# which is the only way to notice a session that is being appended to right now.
FILE_STAT_LIMIT = 400

PUBLISH_INTERVAL = 0.25


@dataclass(frozen=True)
class Scope:
    """Either one working directory, or every project Claude knows about."""

    cwd: str = ""
    all_projects: bool = False

    @classmethod
    def project(cls, abs_dir: str) -> "Scope":
        return cls(cwd=abs_dir, all_projects=False)

    @classmethod
    def everything(cls, abs_dir: str = "") -> "Scope":
        return cls(cwd=abs_dir, all_projects=True)

    @property
    def encoded(self) -> str:
        return paths.encode_dir(self.cwd) if self.cwd else ""

    @property
    def project_dir(self) -> str:
        return os.path.join(paths.PROJECTS_DIR, self.encoded) if self.cwd else ""

    @property
    def label(self) -> str:
        return "all projects" if self.all_projects else self.cwd

    def toggled(self) -> "Scope":
        return Scope(cwd=self.cwd, all_projects=not self.all_projects)

    def project_dirs(self) -> list[str]:
        if not self.all_projects:
            return [self.project_dir]
        try:
            with os.scandir(paths.PROJECTS_DIR) as it:
                return sorted(e.path for e in it if e.is_dir())
        except OSError:
            return []


def _transcripts(project_dir: str) -> list[tuple[str, str, os.stat_result]]:
    """``(sid, path, stat)`` for every transcript in one project directory."""
    out = []
    try:
        with os.scandir(project_dir) as it:
            for entry in it:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                out.append((entry.name[:-len(".jsonl")], entry.path, st))
    except OSError:
        return []
    return out


def _find_elsewhere(sid: str) -> str:
    """The newest transcript for this session in any project directory."""
    matches = glob.glob(os.path.join(paths.PROJECTS_DIR, "*", sid + ".jsonl"))
    if not matches:
        return ""
    try:
        return max(matches, key=os.path.getmtime)
    except OSError:
        return matches[0]


def _stat_ns(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


class Loader:
    """Scans on a background thread; publishes whole :class:`Snapshot` objects."""

    def __init__(self, scope: Scope, sort: Sort = Sort.RECENT, use_cache: bool = True) -> None:
        self._scope = scope
        self._sort = sort
        self._use_cache = use_cache
        self._snap = Snapshot(scanning=True)
        self._raw: tuple | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._fingerprint: tuple = ()

    # -- public API ------------------------------------------------------

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def sort(self) -> Sort:
        return self._sort

    def start(self) -> None:
        if self._thread is not None:
            return
        self._wake.set()
        self._thread = threading.Thread(target=self._run, name="cct-loader", daemon=True)
        self._thread.start()

    def snapshot(self) -> Snapshot:
        return self._snap

    def request_reload(self) -> None:
        with self._lock:
            self._generation += 1
        self._wake.set()

    def set_scope(self, scope: Scope) -> None:
        if scope == self._scope:
            return
        self._scope = scope
        self._raw = None
        self._snap = Snapshot(scanning=True)
        self.request_reload()

    def set_sort(self, sort: Sort) -> None:
        """Reordering needs no disk access, so it happens inline and shows at once."""
        if sort == self._sort:
            return
        self._sort = sort
        raw = self._raw
        if raw is not None:
            self._snap = self._assemble(*raw, scanning=self._snap.scanning,
                                        scanned=self._snap.scanned, total=self._snap.total)
        else:
            self.request_reload()

    def scan_now(self) -> Snapshot:
        """Scan on the calling thread; for one-shot output and for tests."""
        self._scan(self._generation)
        return self._snap

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def dirty(self) -> bool:
        return self._compute_fingerprint() != self._fingerprint

    # -- change detection ------------------------------------------------

    def _compute_fingerprint(self) -> tuple:
        dirs = self._scope.project_dirs()
        parts: list = [_stat_ns(d) for d in dirs]
        parts.append(_stat_ns(paths.ROSTER))
        parts.append(_stat_ns(paths.JOBS_DIR))
        if not self._scope.all_projects and len(dirs) == 1:
            # a single project is small enough that appends are worth noticing
            newest = count = 0
            try:
                with os.scandir(dirs[0]) as it:
                    for entry in it:
                        if not entry.name.endswith(".jsonl"):
                            continue
                        count += 1
                        if count > FILE_STAT_LIMIT:
                            break
                        try:
                            newest = max(newest, entry.stat().st_mtime_ns)
                        except OSError:
                            pass
            except OSError:
                pass
            if count <= FILE_STAT_LIMIT:
                parts.append(newest)
            parts.append(count)
        return tuple(parts)

    # -- scanning --------------------------------------------------------

    def _run(self) -> None:
        while not self._closed.is_set():
            self._wake.wait()
            if self._closed.is_set():
                return
            self._wake.clear()
            with self._lock:
                generation = self._generation
            try:
                self._scan(generation)
            except Exception as exc:  # a scan must never take the UI down with it
                self._snap = Snapshot(
                    sessions=self._snap.sessions,
                    agents=self._snap.agents,
                    jobs=self._snap.jobs,
                    parents=self._snap.parents,
                    children=self._snap.children,
                    roots=self._snap.roots,
                    projects=self._snap.projects,
                    default_branch=self._snap.default_branch,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def _stale(self, generation: int) -> bool:
        return self._closed.is_set() or generation != self._generation

    def _scan(self, generation: int) -> None:
        scope = self._scope
        fingerprint = self._compute_fingerprint()

        prefetch: dict = {}
        worker = threading.Thread(
            target=self._prefetch, args=(scope, prefetch), name="cct-agents", daemon=True
        )
        worker.start()

        dirs = scope.project_dirs()
        entries: list[tuple[str, str, str, os.stat_result]] = []
        for project_dir in dirs:
            for sid, path, st in _transcripts(project_dir):
                entries.append((project_dir, sid, path, st))
        total = len(entries)

        known_jobs = daemon.job_ids()
        jobs = {sid: daemon.job_state(sid)
                for _, sid, _, _ in entries if sid[:8] in known_jobs}
        roster_parents = daemon.roster_fork_parents()

        caches: dict[str, cache_mod.Cache] = {}
        sessions: dict[str, Session] = {}
        last_publish = 0.0
        for index, (project_dir, sid, path, st) in enumerate(entries, 1):
            if self._stale(generation):
                return
            encoded = os.path.basename(os.path.normpath(project_dir))
            cache = caches.get(encoded)
            if cache is None:
                cache = caches[encoded] = cache_mod.Cache(encoded, self._use_cache)
            session = cache.get(sid, path, st)
            if session is None:
                try:
                    session = transcript.parse_session(path, sid)
                except OSError:
                    continue
                cache.put(sid, st, session)
            sessions[sid] = session
            now = time.monotonic()
            if now - last_publish >= PUBLISH_INTERVAL and index < total:
                last_publish = now
                self._publish(sessions, {}, jobs, roster_parents, "",
                              scanning=True, scanned=index, total=total)

        for cache in caches.values():
            cache.save()

        worker.join(timeout=daemon.AGENTS_TIMEOUT + gitinfo.TIMEOUT + 1.0)
        if self._stale(generation):
            return
        agents: list[Agent] = prefetch.get("agents") or []
        default_branch: str = prefetch.get("default_branch") or ""

        self._add_ghosts(sessions, agents, scope, jobs, known_jobs)

        self._fingerprint = fingerprint
        self._publish(sessions, {a.sid: a for a in agents}, jobs, roster_parents,
                      default_branch, scanning=False, scanned=total, total=total)

    def _prefetch(self, scope: Scope, out: dict) -> None:
        out["agents"] = daemon.list_agents(None if scope.all_projects else scope.cwd or None)
        if scope.cwd and not scope.all_projects:
            out["default_branch"] = gitinfo.default_branch(scope.cwd)

    def _add_ghosts(
        self,
        sessions: dict[str, Session],
        agents: list[Agent],
        scope: Scope,
        jobs: dict[str, dict],
        known_jobs: set[str],
    ) -> None:
        """Background sessions the daemon reports but this scope holds no transcript for."""
        pending = [a for a in agents if a.kind == "background" and a.sid not in sessions]
        for agent in pending:
            session = None
            path = _find_elsewhere(agent.sid)
            if path:
                try:
                    session = transcript.parse_session(path, agent.sid)
                except OSError:
                    session = None
            if session is not None:
                if not session.worktree:
                    encoded = os.path.basename(os.path.dirname(path))
                    base = paths.encode_dir(agent.cwd) if scope.all_projects else scope.encoded
                    token = encoded[len(base):] if base and encoded.startswith(base) else encoded
                    session.worktree = token.lstrip("-") or encoded
                if not session.cwd:
                    session.cwd = agent.cwd
            else:
                session = Session(
                    sid=agent.sid,
                    mtime=agent.started_at,
                    first=agent.name,
                    cwd=agent.cwd,
                    ghost=True,
                )
            sessions[agent.sid] = session
            if agent.sid[:8] in known_jobs and agent.sid not in jobs:
                jobs[agent.sid] = daemon.job_state(agent.sid)

    # -- snapshot assembly -----------------------------------------------

    def _publish(self, sessions, agents, jobs, roster_parents, default_branch,
                 *, scanning: bool, scanned: int, total: int) -> None:
        raw = (dict(sessions), dict(agents), dict(jobs), dict(roster_parents), default_branch)
        snap = self._assemble(*raw, scanning=scanning, scanned=scanned, total=total)
        if not scanning:
            self._raw = raw
        self._snap = snap

    def _assemble(self, sessions, agents, jobs, roster_parents, default_branch,
                  *, scanning: bool, scanned: int, total: int) -> Snapshot:
        roots, children, parents = tree.build_forest(
            sessions, jobs, roster_parents, self._sort
        )
        return Snapshot(
            sessions=sessions,
            agents=agents,
            jobs=jobs,
            parents=parents,
            children=children,
            roots=roots,
            projects=_projects(sessions),
            default_branch=default_branch,
            scanning=scanning,
            scanned=scanned,
            total=total,
        )


def _projects(sessions: dict[str, Session]) -> dict[str, Project]:
    """One :class:`Project` per project directory, mapped onto every session in it."""
    by_dir: dict[str, Project] = {}
    for session in sessions.values():
        if not session.project_dir:
            continue
        encoded = os.path.basename(os.path.normpath(session.project_dir))
        project = by_dir.get(encoded)
        if project is None:
            project = by_dir[encoded] = Project(encoded=encoded, path=session.project_dir)
        if not project.cwd and session.cwd:
            project.cwd = session.cwd
            project.label = os.path.basename(session.cwd.rstrip("/")) or session.cwd
    for project in by_dir.values():
        if not project.label:
            project.label = paths.decode_project_dir(project.path).rsplit("/", 1)[-1] \
                or project.encoded
    return {
        session.sid: by_dir[os.path.basename(os.path.normpath(session.project_dir))]
        for session in sessions.values() if session.project_dir
    }


def scan_once(scope: Scope, sort: Sort = Sort.RECENT, use_cache: bool = True) -> Snapshot:
    """Synchronous scan, for ``--print``/``--preview`` and for tests."""
    return Loader(scope, sort, use_cache).scan_now()
