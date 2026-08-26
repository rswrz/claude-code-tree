"""The two git facts the tree needs: which branch is the default, and which is checked out."""

from __future__ import annotations

import subprocess

TIMEOUT = 3.0


def _git(cwd: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ("git", "-C", cwd, *args),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def is_repo(cwd: str) -> bool:
    return _git(cwd, "rev-parse", "--is-inside-work-tree") == "true"


def default_branch(cwd: str) -> str:
    """``origin/HEAD`` when the remote published it, else the first local main/master."""
    head = _git(cwd, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head.startswith("origin/"):
        head = head[len("origin/"):]
    if head:
        return head
    for candidate in ("main", "master"):
        try:
            proc = subprocess.run(
                ("git", "-C", cwd, "show-ref", "--verify", "--quiet",
                 "refs/heads/" + candidate),
                capture_output=True,
                timeout=TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if proc.returncode == 0:
            return candidate
    return ""


def current_branch(cwd: str) -> str:
    if not is_repo(cwd):
        return ""
    return _git(cwd, "branch", "--show-current")
