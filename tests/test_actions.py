"""Actions run against a fake ~/.claude tree and a stubbed `claude` on PATH."""

from __future__ import annotations

import json
import os

import pytest

from cct import actions
from cct.model import Agent, Session, Snapshot
from cct.transcript import parse_session


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    (root / ".claude" / "projects").mkdir(parents=True)
    (root / ".claude" / "jobs").mkdir(parents=True)
    return root


@pytest.fixture
def claude_bin(tmp_path, monkeypatch):
    """A `claude` on PATH that records its arguments and never touches anything."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "claude.log"
    script = bindir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CCT_LOG"\n'
        'if [ "$1" = agents ]; then echo "[]"; fi\n'
        'if [ "$1" = rm ]; then exit "${CCT_RM_RC:-0}"; fi\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("CCT_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def write_session(home, encoded, sid, records):
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return Session(sid=sid, path=str(path), project_dir=str(project_dir), cwd="/tmp/proj")


def fake_paths(monkeypatch, home):
    """session_paths, rooted at the fake home instead of the real one."""
    def session_paths(sid, project_dir, encoded):
        claude = home / ".claude"
        return [
            os.path.join(project_dir, f"{sid}.jsonl"),
            str(claude / "image-cache" / sid),
            str(claude / "session-env" / sid),
            str(claude / "file-history" / sid),
            str(home / "tmp" / encoded / sid),
            str(claude / "jobs" / sid[:8]),
        ]

    monkeypatch.setattr(actions.paths, "session_paths", session_paths, raising=False)
    monkeypatch.setattr(actions.paths, "encode_dir",
                        lambda p: p.replace("/", "-").replace(".", "-"), raising=False)


# --------------------------------------------------------------------------- #
# rename
# --------------------------------------------------------------------------- #


def test_current_title_takes_the_last_record(home):
    session = write_session(home, "-tmp-proj", "s" * 36, [
        {"type": "custom-title", "customTitle": "first"},
        {"type": "user"},
        {"type": "custom-title", "customTitle": "second"},
    ])
    assert actions.current_title(parse_session(session.path)) == "second"


def test_current_title_is_empty_without_a_record(home):
    session = write_session(home, "-tmp-proj", "s" * 36, [{"type": "user"}])
    assert actions.current_title(parse_session(session.path)) == ""


def test_rename_appends_and_round_trips(home):
    session = write_session(home, "-tmp-proj", "s" * 36, [{"type": "user"}])
    assert actions.rename(session, "  a new name  ") is None
    assert actions.current_title(parse_session(session.path)) == "a new name"
    last = json.loads(open(session.path).read().splitlines()[-1])
    assert last == {"type": "custom-title", "customTitle": "a new name", "sessionId": session.sid}


def test_rename_rejects_an_empty_name(home):
    session = write_session(home, "-tmp-proj", "s" * 36, [])
    assert actions.rename(session, "   ") == "empty name"


def test_rename_reports_a_missing_file(home):
    session = Session(sid="x" * 36, path=str(home / "nope.jsonl"))
    assert "not found" in (actions.rename(session, "n") or "")


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_delete_plan_lists_only_existing_paths(home, monkeypatch):
    fake_paths(monkeypatch, home)
    session = write_session(home, "-tmp-proj", "a" * 36, [{"type": "user"}])
    session.custom = "the session"
    (home / ".claude" / "session-env" / session.sid).mkdir(parents=True)
    plan = actions.delete(session, Snapshot())

    assert plan.daemon_known is False
    assert session.path in plan.paths
    assert str(home / ".claude" / "session-env" / session.sid) in plan.paths
    assert str(home / ".claude" / "image-cache" / session.sid) not in plan.paths
    assert plan.lines()[0] == "Session: the session"
    assert f"UUID:    {session.sid}" in plan.lines()


def test_delete_plan_reports_the_background_worktree(home, monkeypatch):
    fake_paths(monkeypatch, home)
    session = write_session(home, "-tmp-proj", "b" * 36, [])
    snap = Snapshot(jobs={session.sid: {"state": "done", "worktreePath": "/wt/x"}})
    plan = actions.delete(session, snap)

    assert plan.daemon_known is True
    assert plan.job_state == "done"
    assert "Background session, state: done" in plan.lines()
    assert "  - /wt/x" in plan.lines()


def test_delete_plan_sees_a_live_background_agent(home, monkeypatch):
    fake_paths(monkeypatch, home)
    session = write_session(home, "-tmp-proj", "c" * 36, [])
    snap = Snapshot(agents={session.sid: Agent(sid=session.sid, kind="background", live=True)})
    assert actions.delete(session, snap).daemon_known is True


def test_perform_delete_removes_files_and_leaves_the_daemon_alone(home, monkeypatch, claude_bin):
    fake_paths(monkeypatch, home)
    session = write_session(home, "-tmp-proj", "d" * 36, [{"type": "user"}])
    report = actions.perform_delete(actions.delete(session, Snapshot()))

    assert not os.path.exists(session.path)
    assert report.steps == []
    assert not claude_bin.exists()
    assert report.lines()[-1] == "deleted."


def test_perform_delete_calls_claude_rm_for_a_background_session(home, monkeypatch, claude_bin):
    fake_paths(monkeypatch, home)
    session = write_session(home, "-tmp-proj", "e" * 36, [])
    snap = Snapshot(jobs={session.sid: {"state": "done"}})
    report = actions.perform_delete(actions.delete(session, snap))

    calls = claude_bin.read_text().splitlines()
    assert calls[0] == f"rm {session.sid[:8]}"
    assert [s.ok for s in report.steps] == [True]
    assert report.agents_view == "removed"


def test_perform_delete_falls_back_to_stop_then_rm(home, monkeypatch, claude_bin):
    fake_paths(monkeypatch, home)
    monkeypatch.setenv("CCT_RM_RC", "3")
    session = write_session(home, "-tmp-proj", "f" * 36, [])
    snap = Snapshot(jobs={session.sid: {"createdAt": "now"}})
    report = actions.perform_delete(actions.delete(session, snap))

    short = session.sid[:8]
    assert claude_bin.read_text().splitlines()[:3] == [f"rm {short}", f"stop {short}", f"rm {short}"]
    assert [s.ok for s in report.steps] == [False, True, False]
    assert "Background session" in actions.delete(session, snap).lines()
    assert not os.path.exists(session.path)


def test_delete_derives_the_encoded_dir_from_the_project_dir(home, monkeypatch):
    seen = {}

    def session_paths(sid, project_dir, encoded):
        seen["encoded"] = encoded
        return []

    monkeypatch.setattr(actions.paths, "session_paths", session_paths, raising=False)
    session = write_session(home, "-Users-r-Projects-thing", "h" * 36, [])
    actions.delete(session, Snapshot())
    assert seen["encoded"] == "-Users-r-Projects-thing"


# --------------------------------------------------------------------------- #
# prune
# --------------------------------------------------------------------------- #


def _stub_session(sid, mtime, stub=True, **kw):
    return Session(sid=sid, mtime=mtime, stub=stub, **kw)


def test_prune_stubs_picks_stubs_newest_first(home, monkeypatch):
    fake_paths(monkeypatch, home)
    now = 1_000_000.0
    snap = Snapshot(sessions={
        "a" * 36: _stub_session("a" * 36, now - 10_000, custom="old stub"),
        "b" * 36: _stub_session("b" * 36, now - 100, custom="fresh stub"),
        "c" * 36: _stub_session("c" * 36, now - 50_000, stub=False),
    })
    rows = actions.prune_stubs(snap, now=now)

    assert [r.sid[:1] for r in rows] == ["b", "a"]
    assert rows[0].fresh is True
    assert rows[1].fresh is False
    assert rows[1].age == "2h ago"


def test_prune_stubs_marks_job_state_and_liveness(home, monkeypatch):
    fake_paths(monkeypatch, home)
    now = 2_000_000.0
    sid = "d" * 36
    snap = Snapshot(
        sessions={sid: _stub_session(sid, now - 10_000)},
        jobs={sid: {"state": "blocked"}},
        agents={sid: Agent(sid=sid, kind="background", live=True)},
    )
    row = actions.prune_stubs(snap, now=now)[0]
    assert row.job == "job:blocked"
    assert row.live is True
    assert row.skipped is True


def test_prune_stubs_ignores_ghosts(home, monkeypatch):
    fake_paths(monkeypatch, home)
    sid = "e" * 36
    snap = Snapshot(sessions={sid: Session(sid=sid, stub=True, ghost=True)})
    assert actions.prune_stubs(snap) == []


def test_perform_prune_skips_fresh_and_live_rows(tmp_path, claude_bin):
    victims = []
    for name in ("gone", "fresh", "live"):
        path = tmp_path / f"{name}.jsonl"
        path.write_text("{}\n")
        victims.append(path)
    rows = [
        actions.StubRow(sid="1" * 36, title="", age="1d ago", job="job:done",
                        paths=[str(victims[0])]),
        actions.StubRow(sid="2" * 36, title="", age="1m ago", fresh=True,
                        paths=[str(victims[1])]),
        actions.StubRow(sid="3" * 36, title="", age="1d ago", live=True,
                        paths=[str(victims[2])]),
    ]
    report = actions.perform_prune(rows)

    assert report.pruned == ["1" * 36]
    assert not victims[0].exists()
    assert victims[1].exists() and victims[2].exists()
    assert claude_bin.read_text().splitlines() == [f"rm {'1' * 8}"]
    assert "touched in the last 15m" in report.skipped[0]
    assert "live in daemon" in report.skipped[1]
    assert report.lines()[-1] == "pruned 1."


def test_perform_prune_leaves_the_cli_alone_without_a_job(tmp_path, claude_bin):
    path = tmp_path / "s.jsonl"
    path.write_text("{}\n")
    actions.perform_prune([actions.StubRow(sid="9" * 36, title="", age="", paths=[str(path)])])
    assert not path.exists()
    assert not claude_bin.exists()


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def test_links_collects_prs_frames_and_job_children(home):
    sid = "k" * 36
    session = write_session(home, "-tmp-proj", sid, [
        {"type": "pr-link", "prNumber": 393, "prRepository": "acme/x", "prUrl": "https://p/393"},
        {"type": "pr-link", "prNumber": 393, "prRepository": "acme/x", "prUrl": "https://p/393"},
        {"type": "frame-link", "frameUrl": "https://a/1", "title": "an artifact"},
        {"type": "user"},
    ])
    snap = Snapshot(jobs={sid: {"children": [
        {"kind": "pr", "id": 400, "href": "https://p/400"},
        {"kind": "note", "href": "https://ignored"},
        {"kind": "pr", "id": 393, "href": "https://p/393"},
    ]}})
    found = actions.links(session, snap)

    assert [link.url for link in found] == ["https://p/393", "https://a/1", "https://p/400"]
    assert found[0].label == "#393 acme/x"
    assert found[1].label == "⧉ an artifact"


def test_links_are_empty_without_a_transcript():
    assert actions.links(Session(sid="z" * 36), Snapshot()) == []


# --------------------------------------------------------------------------- #
# resume / fork
# --------------------------------------------------------------------------- #


def test_resume_command_attaches_when_live():
    session = Session(sid="m" * 36)
    assert actions.resume_command(session, live=True) == ["claude", "attach", "m" * 8]
    assert actions.resume_command(session, live=False) == ["claude", "--resume", "m" * 36]


def test_fork_command_always_resumes():
    session = Session(sid="n" * 36)
    assert actions.fork_command(session) == ["claude", "--resume", "n" * 36, "--fork-session"]


def test_new_command_starts_a_fresh_session():
    assert actions.new_command() == ["claude"]


def test_new_command_names_the_session():
    assert actions.new_command("my session") == ["claude", "--name", "my session"]


def test_new_command_strips_a_blank_name():
    assert actions.new_command("   ") == ["claude"]


def test_dispatch_forwards_the_prompt_and_cwd(monkeypatch):
    calls = []

    def fake_run(args, label="", cwd=None):
        calls.append((args, cwd))
        return actions.Step("claude --bg", True, 0, "queued")

    monkeypatch.setattr(actions, "_run", fake_run)
    step = actions.dispatch("do the thing", "/some/dir")

    assert calls == [(["--bg", "do the thing"], "/some/dir")]
    assert step == actions.Step("claude --bg", True, 0, "queued")


def test_is_live_only_counts_background_agents_with_a_pid():
    sid = "o" * 36
    session = Session(sid=sid)
    assert actions.is_live(session, Snapshot()) is False
    snap = Snapshot(agents={sid: Agent(sid=sid, kind="background", live=False)})
    assert actions.is_live(session, snap) is False
    snap = Snapshot(agents={sid: Agent(sid=sid, kind="background", live=True)})
    assert actions.is_live(session, snap) is True


def test_is_live_interactive_only_counts_interactive_sessions_with_a_pid():
    sid = "u" * 36
    session = Session(sid=sid)
    assert actions.is_live_interactive(session, Snapshot()) is False
    snap = Snapshot(agents={sid: Agent(sid=sid, live=False)})
    assert actions.is_live_interactive(session, snap) is False
    snap = Snapshot(agents={sid: Agent(sid=sid, live=True, status="idle")})
    assert actions.is_live_interactive(session, snap) is True
    snap = Snapshot(agents={sid: Agent(sid=sid, kind="background", live=True)})
    assert actions.is_live_interactive(session, snap) is False


def test_work_dir_prefers_the_session_cwd(tmp_path):
    real = tmp_path / "repo"
    real.mkdir()
    session = Session(sid="p" * 36, cwd=str(real))
    assert actions.work_dir(session, Snapshot(), "/nowhere") == str(real)
    gone = Session(sid="q" * 36, cwd=str(tmp_path / "vanished"))
    assert actions.work_dir(gone, Snapshot(), "/fallback") == "/fallback"


def test_new_dir_uses_the_scope_directory_in_project_scope(tmp_path):
    session = Session(sid="r" * 36, cwd=str(tmp_path))
    assert actions.new_dir(session, Snapshot(), "/scope", all_projects=False) == "/scope"


def test_new_dir_uses_the_selected_session_cwd_in_all_projects_scope(tmp_path):
    real = tmp_path / "repo"
    real.mkdir()
    session = Session(sid="s" * 36, cwd=str(real))
    assert actions.new_dir(session, Snapshot(), "/scope", all_projects=True) == str(real)


def test_new_dir_falls_back_to_scope_without_a_selection():
    assert actions.new_dir(None, Snapshot(), "/scope", all_projects=True) == "/scope"


def test_new_dir_falls_back_to_scope_when_the_session_cwd_is_gone(tmp_path):
    gone = Session(sid="t" * 36, cwd=str(tmp_path / "vanished"))
    assert actions.new_dir(gone, Snapshot(), "/scope", all_projects=True) == "/scope"
