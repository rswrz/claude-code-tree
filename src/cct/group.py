"""Partitioning the root list into labelled blocks.

Purely a display dimension: it reorders roots and never touches parent
inference or the sort keys. Roots keep their relative order inside a block, and
a root decides its whole tree's block — a fork can land under a different
project directory than its parent, and a split tree has no skeleton left.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from . import paths
from .model import Group, Snapshot
from .tree import subtree_mtime

__all__ = ["Block", "AGE_LABELS", "UNKNOWN_PROJECT", "partition"]

DAY = 86400.0

# rolling windows, so the labels name spans and not calendar days
_AGE_WINDOWS = (("last day", DAY), ("last week", 7 * DAY), ("last month", 30 * DAY))
_AGE_REST = "older"

#: every age label, youngest window first
AGE_LABELS = tuple(label for label, _ in _AGE_WINDOWS) + (_AGE_REST,)

# parenthesised, so it reads as no name rather than as a directory called
# unknown, which keeps a real repo of that name short
UNKNOWN_PROJECT = "(unknown)"

# the repo's own worktrees live below this, one project directory each
_WORKTREES = "/.claude/worktrees/"


@dataclass(frozen=True)
class Block:
    key: str                  # '' for Group.NONE and for the unknown project
    label: str                # '' means: draw no header
    roots: list[str]


def _repo_of(cwd: str) -> str:
    """The repo a cwd belongs to: everything below a worktree folds into it."""
    # the filesystem root is a repo like any other, and keeps its slash
    return cwd.partition(_WORKTREES)[0].rstrip("/") or "/"


def _cwd_of(snap: Snapshot, sid: str) -> str:
    """Where a root ran, as far as anything says.

    Its own cwd decides: sessions of one project directory need not share a
    working directory, and ``Project.cwd`` names whichever of them the store
    happened to read first. A ghost session holds no project directory at all,
    and the daemon still told us where it ran.
    """
    session = snap.sessions.get(sid)
    project = snap.project_of(sid)
    return (session.cwd if session else "") or (project.cwd if project else "")


def _repo_owners(cwds: list[str]) -> dict[str, str]:
    """The repo behind a project-directory name, where the encoding pins it down.

    ``encode_dir`` is a function, so a repo path that encodes to a project
    directory's name is that directory's repo — known, not guessed, which lets
    a session with no cwd of its own join the repo's block. Two repo paths can
    encode alike, ``-`` standing for ``/``, ``.`` and ``-`` at once; then the
    name pins down nothing and no owner is recorded.
    """
    owners: dict[str, str] = {}
    ambiguous: set[str] = set()
    for cwd in cwds:
        if not cwd:
            continue
        repo = _repo_of(cwd)
        encoded = paths.encode_dir(repo)
        if owners.setdefault(encoded, repo) != repo:
            ambiguous.add(encoded)
    for encoded in ambiguous:
        del owners[encoded]
    return owners


def _project_of(snap: Snapshot, sid: str, cwd: str,
                owners: dict[str, str]) -> tuple[str, str]:
    """The key and label of the block a root belongs to."""
    if not cwd:
        project = snap.project_of(sid)
        encoded = project.encoded if project is not None else ""
        cwd = owners.get(encoded, "")
        if not cwd:
            if not encoded:
                # nothing names it, and the empty key belongs to this bucket alone
                return "", UNKNOWN_PROJECT
            # decoding would have to invent which of the dashes were slashes,
            # so the directory name stands for itself
            return encoded, encoded
    # the whole repo path is the key, since two different repos can share a
    # basename; the label follows it, and not Project.name, which still names
    # the worktree the session ran in
    key = _repo_of(cwd)
    return key, key.rsplit("/", 1)[-1] or key


def _age_of(diff: float) -> tuple[str, str]:
    for label, window in _AGE_WINDOWS:
        if diff < window:
            return label, label
    return _AGE_REST, _AGE_REST


def _tail(key: str, depth: int) -> str:
    """The last ``depth`` path segments of a key."""
    return "/".join(key.strip("/").split("/")[-depth:])


def _disambiguate(blocks: list[Block]) -> list[Block]:
    """Grow labels that collide leftwards along their key until they differ.

    Blocks are keyed by the whole repo path and so already stand apart; two
    repos sharing a basename would merely look alike. Only a colliding label
    grows, and only while it still collides, so a unique label beside a
    collision stays short.
    """
    labels = [block.label for block in blocks]
    depths = [1] * len(blocks)
    while True:
        clashing = [i for i, label in enumerate(labels) if labels.count(label) > 1]
        if not clashing:
            break
        grown = False
        for i in clashing:
            key = blocks[i].key
            if depths[i] < len(key.strip("/").split("/")):
                depths[i] += 1
                labels[i] = _tail(key, depths[i])
                grown = True
        if not grown:
            # segments ran out; keys are unique where their labels are not
            for i in clashing:
                labels[i] = blocks[i].key or labels[i]
            break
    return [replace(block, label=label) for block, label in zip(blocks, labels)]


def partition(
    snap: Snapshot,
    roots: list[str],
    children: dict[str, list[str]],
    mode: Group,
    now: float | None = None,
) -> list[Block]:
    """Split an already-ordered root list into blocks, keeping their order."""
    if mode is Group.NONE:
        return [Block(key="", label="", roots=list(roots))]

    if mode is Group.PROJECT:
        cwds = [_cwd_of(snap, sid) for sid in roots]
        owners = _repo_owners(cwds)
        keyed = [_project_of(snap, sid, cwd, owners) for sid, cwd in zip(roots, cwds)]
    else:
        # a root ages by its whole subtree, the same key the default sort
        # ranks it by, so the blocks come out in one run each
        recent = subtree_mtime(snap.sessions, children, roots)
        clock = time.time() if now is None else now
        keyed = [_age_of(clock - recent[sid]) for sid in roots]

    order: list[str] = []
    labels: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    for sid, (key, label) in zip(roots, keyed):
        if key not in members:
            order.append(key)
            labels[key] = label
            members[key] = []
        members[key].append(sid)
    if mode is Group.AGE:
        # age windows have an intrinsic order, so they hold it under every sort;
        # projects have none, and their blocks inherit the active sort by
        # appearing in the order their first root does
        order.sort(key=AGE_LABELS.index)
    return _disambiguate(
        [Block(key=key, label=labels[key], roots=members[key]) for key in order]
    )
