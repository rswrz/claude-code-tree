"""Shared pytest fixtures: a fake ``~/.claude`` tree, and a stubbed ``claude`` CLI.

Nothing here ever touches a real Claude installation. ``fake_home`` points
``cct.paths`` at a throwaway directory via the same ``CLAUDE_CONFIG_DIR``
override the real CLI honours; every other fixture builds on top of it.
"""

from __future__ import annotations

import json
import os
import stat
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from cct import paths

DEFAULT_MODEL = "claude-sonnet-5-20260101"


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway ``~/.claude`` layout, with ``cct.paths`` repointed at it."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    (claude_dir / "projects").mkdir(parents=True)
    (claude_dir / "daemon").mkdir(parents=True)
    (claude_dir / "jobs").mkdir(parents=True)
    (claude_dir / "daemon" / "roster.json").write_text(json.dumps({"workers": {}}))

    cache_home = tmp_path / "cache"
    cache_home.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    paths.reload()

    return home


@pytest.fixture
def fake_project(fake_home: Path, tmp_path: Path) -> Callable[..., Path]:
    """Factory for a ``~/.claude/projects/<encoded>`` directory.

    ``fake_project()`` invents a cwd; ``fake_project(cwd=...)`` controls the
    encoded name.
    """

    def _make(cwd: str | None = None) -> Path:
        project_root = Path(cwd) if cwd else tmp_path / "project"
        project_root.mkdir(parents=True, exist_ok=True)
        abs_dir = str(project_root.resolve())
        project_dir = Path(paths.project_dir_for(abs_dir))
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir

    return _make


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_records(
    sid: str,
    *,
    cwd: str,
    branch: str,
    model: str,
    turns: int,
    start: datetime,
) -> list[dict[str, Any]]:
    """A minimal, valid pair of user/assistant turns per requested turn count."""
    records: list[dict[str, Any]] = []
    for i in range(turns):
        u_ts = start + timedelta(minutes=2 * i)
        a_ts = start + timedelta(minutes=2 * i + 1)
        records.append(
            {
                "type": "user",
                "uuid": f"{sid}-u{i}",
                "gitBranch": branch,
                "cwd": cwd,
                "timestamp": _iso(u_ts),
                "isSidechain": False,
                "isMeta": False,
                "message": {"role": "user", "content": f"test message {i}"},
            }
        )
        records.append(
            {
                "type": "assistant",
                "uuid": f"{sid}-a{i}",
                "gitBranch": branch,
                "cwd": cwd,
                "timestamp": _iso(a_ts),
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": f"reply {i}",
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
        )
    return records


@pytest.fixture
def make_transcript() -> Callable[..., Path]:
    """Factory: write a synthetic ``<sid>.jsonl`` transcript into a project directory.

    Pass ``records`` (a list of already-shaped JSONL dicts) for full control
    over a test's fixture data. Without it, a minimal transcript with
    ``turns`` user/assistant pairs is generated.
    """

    def _make(
        project_dir: Path,
        sid: str,
        records: list[dict[str, Any]] | None = None,
        *,
        cwd: str | None = None,
        branch: str = "main",
        model: str = DEFAULT_MODEL,
        turns: int = 1,
        start: datetime | None = None,
    ) -> Path:
        project_dir = Path(project_dir)
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{sid}.jsonl"
        if records is None:
            records = _default_records(
                sid,
                cwd=cwd or str(project_dir),
                branch=branch,
                model=model,
                turns=turns,
                start=start or datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
            )
        with path.open("w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        return path

    return _make


@pytest.fixture
def make_job_state(fake_home: Path) -> Callable[..., Path]:
    """Factory: write ``~/.claude/jobs/<sid8>/state.json`` for a background session."""

    def _make(sid: str, **fields: Any) -> Path:
        job_dir = Path(paths.job_dir(sid))
        job_dir.mkdir(parents=True, exist_ok=True)
        state = {"state": "done"} | fields
        (job_dir / "state.json").write_text(json.dumps(state))
        return job_dir / "state.json"

    return _make


class StubClaude:
    """Handle to the fake ``claude`` executable installed on ``PATH`` by ``stub_claude``."""

    def __init__(self, script: Path, log: Path, agents_json: Path) -> None:
        self.path = script
        self.log = log
        self.agents_json = agents_json
        self.agents_json.write_text("[]")

    def set_agents(self, agents: list[dict[str, Any]]) -> None:
        """Control what ``claude agents --json --all`` prints on its next call."""
        self.agents_json.write_text(json.dumps(agents))

    def calls(self) -> list[list[str]]:
        """Every invocation's argv (excluding the ``claude`` binary itself), in order."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


@pytest.fixture
def stub_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StubClaude:
    """Put a stubbed ``claude`` executable on ``PATH`` so tests never invoke the real CLI.

    It logs every invocation (readable via ``StubClaude.calls()``) and answers
    ``claude agents --json --all [--cwd DIR]`` from a JSON file the test
    controls via ``StubClaude.set_agents()`` (default: ``[]``). Every other
    subcommand (``rm``, ``stop``, ``attach``, ``--resume``, ...) succeeds
    silently, which is enough for code that only checks the exit status.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "claude-calls.log"
    agents_json_path = tmp_path / "claude-agents.json"

    script = bin_dir / "claude"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys

            LOG = {str(log_path)!r}
            AGENTS_JSON = {str(agents_json_path)!r}

            args = sys.argv[1:]
            with open(LOG, "a") as fh:
                fh.write(json.dumps(args) + "\\n")

            if args[:2] == ["agents", "--json"]:
                with open(AGENTS_JSON) as fh:
                    sys.stdout.write(fh.read())
                sys.exit(0)

            sys.exit(0)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    return StubClaude(script, log_path, agents_json_path)
