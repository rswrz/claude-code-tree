"""Where Claude keeps its state on disk, and how it encodes directory names.

Every location is a module global, recomputed by :func:`reload` after ``HOME`` or
``CLAUDE_CONFIG_DIR`` changes. Reach them through the module
(``paths.PROJECTS_DIR``), never by ``from ... import``: a rebound global does not
reach the importer.
"""

from __future__ import annotations

import glob
import os

HOME: str = ""
CLAUDE_DIR: str = ""
PROJECTS_DIR: str = ""
JOBS_DIR: str = ""
DAEMON_DIR: str = ""
ROSTER: str = ""
CACHE_DIR: str = ""


def reload() -> None:
    """Recompute every location from the current environment."""
    global HOME, CLAUDE_DIR, PROJECTS_DIR, JOBS_DIR, DAEMON_DIR, ROSTER, CACHE_DIR
    HOME = os.path.expanduser("~")
    CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
    JOBS_DIR = os.path.join(CLAUDE_DIR, "jobs")
    DAEMON_DIR = os.path.join(CLAUDE_DIR, "daemon")
    ROSTER = os.path.join(DAEMON_DIR, "roster.json")
    CACHE_DIR = os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"),
        "claude-code-tree",
    )


reload()


def encode_dir(path: str) -> str:
    """Claude's project-directory encoding: ``/`` and ``.`` both become ``-``."""
    return path.replace("/", "-").replace(".", "-")


# a repo's own worktrees live below this, one project directory each
WORKTREES = "/.claude/worktrees/"


def repo_of(cwd: str) -> str:
    """The repo a cwd belongs to: everything below a worktree folds into it."""
    if not cwd:
        return ""
    # the filesystem root is a repo like any other, and keeps its slash
    return cwd.partition(WORKTREES)[0].rstrip("/") or "/"


def project_dir_for(abs_dir: str) -> str:
    return os.path.join(PROJECTS_DIR, encode_dir(abs_dir))


def encoded_of(project_dir: str) -> str:
    """The ``<encoded>`` name of a project directory, however it was spelled."""
    return os.path.basename(os.path.normpath(project_dir))


def decode_project_dir(project_dir: str) -> str:
    """Best-effort inverse of :func:`encode_dir`; only the leading ``/`` is certain."""
    enc = encoded_of(project_dir)
    return "/" + enc.lstrip("-").replace("-", "/") if enc else ""


def find_transcript(sid: str) -> str:
    """The newest transcript for this session in any project directory."""
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", sid + ".jsonl"))
    if not matches:
        return ""
    try:
        return max(matches, key=os.path.getmtime)
    except OSError:   # a file may vanish between the glob and the stat
        return matches[0]


def session_paths(sid: str, project_dir: str, encoded: str) -> list[str]:
    """Every file the session owns, in the order the delete dialog lists them."""
    return [
        os.path.join(project_dir, sid + ".jsonl"),
        os.path.join(CLAUDE_DIR, "image-cache", sid),
        os.path.join(CLAUDE_DIR, "session-env", sid),
        os.path.join(CLAUDE_DIR, "file-history", sid),
        os.path.join("/private/tmp", "claude-%d" % os.getuid(), encoded, sid),
        os.path.join(JOBS_DIR, sid[:8]),
    ]


def job_dir(sid: str) -> str:
    return os.path.join(JOBS_DIR, sid[:8])


def resolve_dir(target: str) -> str:
    path = os.path.realpath(os.path.expanduser(target))
    if not os.path.isdir(path):
        raise NotADirectoryError(f"not a directory: {target}")
    return path
