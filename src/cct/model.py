"""Shared data types. Nothing else crosses module borders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass
class Session:
    """One Claude transcript, plus whatever the daemon knows about it."""

    sid: str
    path: str = ""            # transcript file, '' for a daemon-only ghost
    project_dir: str = ""     # ~/.claude/projects/<encoded>
    cwd: str = ""             # working directory the session ran in
    mtime: float = 0.0
    uuids: list[str] = field(default_factory=list)
    count: int = 0            # user + assistant records
    users: int = 0            # real user turns (no meta, no sidechain)
    first: str = ""           # first user message, trimmed
    custom: str = ""          # custom-title record
    ai: str = ""              # ai-title record
    branch: str = ""
    worktree: str = ""        # worktree name, or relocated basename
    prs: set[int] = field(default_factory=set)
    frames: int = 0           # artifact links
    ctx: int | None = None    # context tokens of the last main-chain turn
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    stub: bool = False        # transcript holds no turns, not resumable
    ghost: bool = False       # daemon knows it, no transcript found
    hay: str = ""             # search haystack (message bodies)

    @property
    def title(self) -> str:
        return self.custom or self.ai or self.first or self.sid[:8]


@dataclass
class Agent:
    """A row of ``claude agents --json``."""

    sid: str
    kind: str = ""            # 'background' for daemon jobs
    state: str = ""           # background: done, blocked, stopped, failed, ...
    status: str = ""          # interactive: working, waiting, ...
    live: bool = False        # has a pid
    name: str = ""
    started_at: float = 0.0   # epoch seconds
    cwd: str = ""             # directory the daemon launched it in


@dataclass
class Project:
    """A ~/.claude/projects/<encoded> directory."""

    encoded: str
    path: str                 # the project directory itself
    cwd: str = ""             # best known real working directory
    label: str = ""           # what the project column shows

    @property
    def name(self) -> str:
        return self.label or self.cwd.rstrip("/").rsplit("/", 1)[-1] or self.encoded


class Sort(Enum):
    """Ordering of roots and of siblings within a tree."""

    RECENT = "recent"         # newest session anywhere in the subtree, first
    TURNS = "turns"
    CONTEXT = "context"
    DURATION = "duration"
    NAME = "name"

    @property
    def label(self) -> str:
        return self.value

    def next(self) -> "Sort":
        members = list(Sort)
        return members[(members.index(self) + 1) % len(members)]


@dataclass
class Snapshot:
    """An immutable view of the world, handed from the loader to the UI."""

    sessions: dict[str, Session] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    jobs: dict[str, dict] = field(default_factory=dict)   # keyed by full sid
    parents: dict[str, str | None] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    projects: dict[str, Project] = field(default_factory=dict)  # keyed by sid
    default_branch: str = ""
    scanning: bool = False
    scanned: int = 0
    total: int = 0
    error: str = ""

    def project_of(self, sid: str) -> Project | None:
        return self.projects.get(sid)


@dataclass
class TreeRow:
    """One rendered line of the forest, before column layout."""

    sid: str
    prefix: str               # the tree skeleton left of the connector
    connector: str            # '├─ ', '└─ ' or ''
    depth: int
