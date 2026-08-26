"""Preview pane content, ordering and wrapping."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cct import fuzzy, paths, preview
from cct.layout import build_rows
from cct.model import Agent, Session, Snapshot, TreeRow
from cct.theme import Row, Style, plain

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def iso(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def user(text: str, n: int = 0, **kw) -> dict:
    return {"type": "user", "uuid": f"u{n}", "timestamp": iso(n), "cwd": "/repo",
            "gitBranch": "main", "message": {"role": "user", "content": text}} | kw


def assistant(text: str, n: int = 0, **kw) -> dict:
    return {"type": "assistant", "uuid": f"a{n}", "timestamp": iso(n), "cwd": "/repo",
            "gitBranch": "main", "version": "2.1.206",
            "message": {"role": "assistant", "model": "claude-opus-5-20260101",
                        "content": text,
                        "usage": {"input_tokens": 100, "cache_read_input_tokens": 400}}} | kw


def tool_turn(n: int = 0, calls: int = 1) -> dict:
    blocks = [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}} for i in range(calls)]
    return {"type": "assistant", "uuid": f"t{n}", "timestamp": iso(n), "cwd": "/repo",
            "message": {"role": "assistant", "model": "claude-opus-5-20260101", "content": blocks}}


@pytest.fixture
def worker(fake_home):
    """Give sessions a live daemon worker, the way the roster records one."""

    def _make(*sids: str) -> None:
        Path(paths.ROSTER).write_text(json.dumps(
            {"workers": {f"w{i}": {"sessionId": sid} for i, sid in enumerate(sids)}}))

    return _make


@pytest.fixture
def transcript(fake_project, make_transcript):
    project_dir = fake_project()

    def _make(records: list[dict], sid: str = SID):
        make_transcript(project_dir, sid, records)
        return sid

    return _make


def text_of(rows: list[Row]) -> list[str]:
    return [plain(row) for row in rows]


def kv(rows: list[Row], key: str) -> str | None:
    for line in text_of(rows):
        if line.startswith(key.ljust(10)) and len(line) > 10:
            return line[10:]
    return None


def badge_span(row: Row) -> tuple[str, Style | None]:
    """The badge span of one rendered row, or ``('', None)`` when it carries none."""
    for text, style in row:
        token = text.strip()
        if token.startswith(("bg:", "live:")):
            return token, style
    return "", None


def row_badge(snap: Snapshot, sid: str, width: int = 120) -> tuple[str, Style | None]:
    """The badge the session list draws for ``sid``."""
    tree = [TreeRow(sid=sid, prefix="", connector="", depth=0)]
    return badge_span(build_rows(snap, tree, width, False)[0])


def pane_badge(rows: list[Row]) -> tuple[str, Style | None]:
    """The badge the preview pane draws."""
    for row in rows:
        found = badge_span(row)
        if found[0]:
            return found
    return "", None


class TestHeader:
    def test_custom_title_wins(self, transcript):
        sid = transcript([
            {"type": "ai-title", "aiTitle": "ai name"},
            {"type": "custom-title", "customTitle": "my name"},
            user("hello"),
        ])
        assert text_of(preview.render(sid, Snapshot()))[0] == "my name"

    def test_ai_title_is_next(self, transcript):
        sid = transcript([{"type": "ai-title", "aiTitle": "ai name"}, user("hello")])
        assert text_of(preview.render(sid, Snapshot()))[0] == "ai name"

    def test_then_the_first_user_message(self, transcript):
        sid = transcript([user("what happened to the deploy")])
        assert text_of(preview.render(sid, Snapshot()))[0] == "what happened to the deploy"

    def test_and_finally_the_short_sid(self, transcript):
        sid = transcript([tool_turn()])
        assert text_of(preview.render(sid, Snapshot()))[0] == sid[:8]

    def test_the_title_is_bold(self, transcript):
        sid = transcript([user("hello")])
        assert preview.render(sid, Snapshot())[0][0][1] is Style.BOLD

    def test_stats_line(self, transcript):
        sid = transcript([user("hi", 0), assistant("there", 30)])
        assert re.fullmatch(r"1 turns · 500 ctx · 30m · \d+[smhdy]o? ago",
                            text_of(preview.render(sid, Snapshot()))[1])

    def test_a_transcript_without_turns_says_so(self, transcript):
        sid = transcript([{"type": "custom-title", "customTitle": "stub"}])
        lines = text_of(preview.render(sid, Snapshot()))
        assert "∅ transcript holds no messages — this session cannot be resumed" in lines

    def test_the_job_badge_follows_the_stats(self, transcript):
        sid = transcript([user("hi")])
        snap = Snapshot(agents={sid: Agent(sid=sid, kind="background", state="blocked")})
        rows = preview.render(sid, snap)
        assert text_of(rows)[2] == "bg:blocked"
        assert rows[2][0][1] is Style.STATE_WARN

    def test_the_status_comes_from_the_snapshot(self, transcript):
        """Only the agents view knows a status, and the pane spells it."""
        sid = transcript([user("hi")])
        snap = Snapshot(agents={sid: Agent(sid=sid, status="working", live=True)})
        rows = preview.render(sid, snap)
        assert text_of(rows)[2] == "live:working"
        assert rows[2][0][1] is Style.STATE_OK

    def test_the_daemon_files_alone_earn_no_badge(self, transcript, make_job_state, worker):
        """No agent in the snapshot, no badge — the same nothing the row shows."""
        sid = transcript([user("hi")])
        make_job_state(sid, state="working")
        worker(sid)
        snap = Snapshot(jobs={sid: {"state": "working"}})
        assert pane_badge(preview.render(sid, snap)) == ("", None)
        assert row_badge(snap, sid) == ("", None)

    def test_the_state_row_cannot_contradict_the_badge(self, transcript, make_job_state):
        """Badge and state row both come from the snapshot, so they speak as one."""
        sid = transcript([user("hi")])
        make_job_state(sid, state="working")           # the disk is behind the snapshot
        snap = Snapshot(
            agents={sid: Agent(sid=sid, kind="background", state="done")},
            jobs={sid: {"state": "done"}},
        )
        rows = preview.render(sid, snap)
        assert pane_badge(rows) == ("bg:done", Style.STATE_DONE)
        assert kv(rows, "state") == "done"

    def test_a_session_the_daemon_knows_nothing_about_wears_no_badge(self, transcript):
        sid = transcript([user("hi")])
        lines = text_of(preview.render(sid, Snapshot()))
        assert lines[2].startswith("session ")


class TestBadgeAgreement:
    """The pane and the row beside it spell one badge, colour included."""

    LIVE_STATUS = "11111111-1111-1111-1111-111111111111"
    LIVE_BARE = "22222222-2222-2222-2222-222222222222"
    BG_STATE = "33333333-3333-3333-3333-333333333333"
    BG_STALLED = "44444444-4444-4444-4444-444444444444"
    UNKNOWN = "55555555-5555-5555-5555-555555555555"
    LIVE_NO_WORKER = "66666666-6666-6666-6666-666666666666"

    @pytest.fixture
    def world(self, transcript, make_job_state, worker):
        """Every badge shape at once: one snapshot, and the disk behind it."""
        sids = [self.LIVE_STATUS, self.LIVE_BARE, self.BG_STATE,
                self.BG_STALLED, self.UNKNOWN, self.LIVE_NO_WORKER]
        for sid in sids:
            transcript([user("hi")], sid)
        make_job_state(self.BG_STATE, state="done")
        make_job_state(self.BG_STALLED, state="working")
        worker(self.LIVE_STATUS, self.LIVE_BARE, self.BG_STATE)
        return Snapshot(
            sessions={sid: Session(sid=sid, mtime=1_000_000.0, users=1) for sid in sids},
            agents={
                self.LIVE_STATUS: Agent(sid=self.LIVE_STATUS, status="working", live=True),
                self.LIVE_BARE: Agent(sid=self.LIVE_BARE, live=True),
                self.BG_STATE: Agent(sid=self.BG_STATE, kind="background",
                                     state="done", live=True),
                self.BG_STALLED: Agent(sid=self.BG_STALLED, kind="background",
                                       state="working"),
                self.LIVE_NO_WORKER: Agent(sid=self.LIVE_NO_WORKER, status="waiting",
                                           live=True),
            },
            jobs={self.BG_STATE: {"state": "done"},
                  self.BG_STALLED: {"state": "working"}},
        )

    # LIVE_STATUS and LIVE_NO_WORKER are the shapes only the snapshot can spell:
    # a status, and a session no daemon file mentions. The rest guard agreement.
    @pytest.mark.parametrize("sid,expected", [
        ("LIVE_STATUS", ("live:working", Style.STATE_OK)),
        ("LIVE_NO_WORKER", ("live:waiting", Style.STATE_WARN)),
        ("LIVE_BARE", ("live:", Style.STATE_OK)),
        ("BG_STATE", ("bg:done", Style.STATE_DONE)),
        ("BG_STALLED", ("bg:working", Style.STATE_BAD)),
        ("UNKNOWN", ("", None)),
    ])
    def test_one_snapshot_one_badge(self, world, sid, expected):
        sid = getattr(self, sid)
        assert row_badge(world, sid) == expected
        assert pane_badge(preview.render(sid, world)) == expected

    def test_a_job_without_a_transcript_agrees_too(self, fake_home, make_job_state):
        """The job view draws the snapshot's agent as well, liveness included."""
        make_job_state(SID, state="working", name="nightly")
        snap = Snapshot(agents={SID: Agent(sid=SID, kind="background",
                                           state="working", live=True)})
        assert pane_badge(preview.render(SID, snap)) == row_badge(snap, SID)
        assert pane_badge(preview.render(SID, snap)) == ("bg:working", Style.STATE_OK)

    def test_a_job_without_an_agent_keeps_its_state_row(self, fake_home, make_job_state):
        """No badge left to carry the state, so the job view spells it out."""
        make_job_state(SID, state="working", name="nightly")
        rows = preview.render(SID, Snapshot())
        assert pane_badge(rows) == ("", None)
        assert "state working" in text_of(rows)


class TestKeyValues:
    def test_session_id_comes_first(self, transcript):
        sid = transcript([user("hi")])
        assert kv(preview.render(sid, Snapshot()), "session") == sid

    def test_model_is_shortened(self, transcript):
        sid = transcript([assistant("hi", message={
            "role": "assistant", "model": "claude-sonnet-4-5-20250929", "content": "hi"})])
        assert kv(preview.render(sid, Snapshot()), "model") == "sonnet-4.5"

    def test_cwd_branch_and_cli(self, transcript):
        sid = transcript([user("hi"), assistant("ho")])
        rows = preview.render(sid, Snapshot())
        assert kv(rows, "cwd") == "/repo"
        assert kv(rows, "branch") == "main"
        assert kv(rows, "cli") == "2.1.206"

    def test_modes(self, transcript):
        sid = transcript([
            {"type": "agent-setting", "agentSetting": "fable"},
            {"type": "mode", "mode": "plan"},
            {"type": "permission-mode", "permissionMode": "acceptEdits"},
            user("hi", 0, effort="high"),
        ])
        rows = preview.render(sid, Snapshot())
        assert kv(rows, "agent") == "fable"
        assert kv(rows, "mode") == "plan"
        assert kv(rows, "perm") == "acceptEdits"
        assert kv(rows, "effort") == "high"

    def test_worktree_pairs_name_and_path(self, transcript):
        sid = transcript([
            {"type": "worktree-state",
             "worktreeSession": {"worktreeName": "wt", "worktreePath": "/wt"}},
            user("hi"),
        ])
        assert kv(preview.render(sid, Snapshot()), "worktree") == "wt /wt"

    def test_relocated_is_hidden_when_it_repeats_the_cwd(self, transcript):
        sid = transcript([{"type": "relocated", "relocatedCwd": "/repo"}, user("hi")])
        assert kv(preview.render(sid, Snapshot()), "relocated") is None

    def test_relocated_is_shown_when_it_differs(self, transcript):
        sid = transcript([{"type": "relocated", "relocatedCwd": "/elsewhere"}, user("hi")])
        assert kv(preview.render(sid, Snapshot()), "relocated") == "/elsewhere"

    def test_prs_are_listed_with_their_urls(self, transcript):
        sid = transcript([
            {"type": "pr-link", "prNumber": 353, "prRepository": "acme/widgets",
             "prUrl": "https://example.test/pull/353"},
            user("hi"),
        ])
        lines = text_of(preview.render(sid, Snapshot()))
        i = lines.index("pr        #353 acme/widgets")
        assert lines[i + 1].strip() == "https://example.test/pull/353"

    def test_artifacts_are_listed_with_their_urls(self, transcript):
        sid = transcript([
            {"type": "frame-link", "title": "report", "frameUrl": "https://example.test/a"},
            user("hi"),
        ])
        lines = text_of(preview.render(sid, Snapshot()))
        i = lines.index("artifact  report")
        assert lines[i + 1].strip() == "https://example.test/a"

    def test_touched_files_are_counted(self, transcript):
        sid = transcript([
            {"type": "file-history-delta", "trackingPath": "/repo/a.py"},
            {"type": "file-history-delta", "trackingPath": "/repo/b.py"},
            {"type": "file-history-delta", "trackingPath": "/repo/a.py"},
            user("hi"),
        ])
        assert kv(preview.render(sid, Snapshot()), "files") == "2 files touched"

    def test_skills_are_collected_in_order(self, transcript):
        sid = transcript([
            user("hi", 0, attributionSkill="design"),
            user("ho", 1, attributionSkill="dataviz"),
            user("he", 2, attributionSkill="design"),
        ])
        assert kv(preview.render(sid, Snapshot()), "skills") == "design, dataviz"

    def test_job_fields_come_last(self, transcript):
        sid = transcript([user("hi")])
        snap = Snapshot(jobs={sid: {"state": "done", "intent": "ship it",
                                    "detail": "all green", "tokens": 1234,
                                    "createdAt": "t0", "updatedAt": "t1"}})
        rows = preview.render(sid, snap)
        assert kv(rows, "intent") == "ship it"
        assert kv(rows, "detail") == "all green"
        assert kv(rows, "tokens") == "1234"
        lines = text_of(rows)
        assert lines.index("state     done") > lines.index("session   " + sid)

    def test_empty_values_are_skipped(self, transcript):
        sid = transcript([user("hi")])
        rows = preview.render(sid, Snapshot())
        assert kv(rows, "mode") is None
        assert kv(rows, "worktree") is None


class TestMessages:
    def test_user_and_agent_turns_are_labelled(self, transcript):
        sid = transcript([user("question", 0), assistant("answer", 1)])
        rows = preview.render(sid, Snapshot())
        assert ["▍User question", "▍Agent answer"] == [
            line for line in text_of(rows) if line.startswith("▍")
        ]

    def test_turn_labels_are_coloured(self, transcript):
        sid = transcript([user("question", 0), assistant("answer", 1)])
        rows = [r for r in preview.render(sid, Snapshot()) if r and r[0][0].startswith("▍")]
        assert rows[0][0][1] is Style.USER
        assert rows[1][0][1] is Style.AGENT

    def test_consecutive_tool_calls_collapse_into_one_line(self, transcript):
        sid = transcript([tool_turn(0, calls=2), tool_turn(1, calls=3), assistant("done", 2)])
        assert "· 5 tool calls" in text_of(preview.render(sid, Snapshot()))

    def test_a_single_tool_call_is_singular(self, transcript):
        sid = transcript([tool_turn(0, calls=1), assistant("done", 1)])
        assert "· 1 tool call" in text_of(preview.render(sid, Snapshot()))

    def test_only_the_last_thirty_messages_survive(self, transcript):
        sid = transcript([user(f"message {i}", i) for i in range(40)])
        shown = [line for line in text_of(preview.render(sid, Snapshot()))
                 if line.startswith("▍User")]
        assert len(shown) == preview.MSG_KEEP
        assert shown[0] == "▍User message 10"

    def test_long_messages_are_truncated(self, transcript):
        sid = transcript([user("x" * 900)])
        line = [l for l in text_of(preview.render(sid, Snapshot())) if l.startswith("▍User")][0]
        assert line.endswith("…")
        assert len(line) == len("▍User ") + preview.MSG_MAX

    def test_meta_and_bracketed_turns_are_hidden(self, transcript):
        sid = transcript([
            user("real", 0),
            user("meta", 1, isMeta=True),
            user("<command-name>x</command-name>", 2),
        ])
        shown = [l for l in text_of(preview.render(sid, Snapshot())) if l.startswith("▍")]
        assert shown == ["▍User real"]

    def test_whitespace_is_collapsed(self, transcript):
        sid = transcript([user("a\n\n   b\tc")])
        assert "▍User a b c" in text_of(preview.render(sid, Snapshot()))

    def test_text_blocks_are_joined(self, transcript):
        sid = transcript([user([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])])
        assert "▍User one two" in text_of(preview.render(sid, Snapshot()))


class TestQuery:
    def records(self):
        return [user(f"deploy attempt {i}" if i % 2 else f"nothing here {i}", i)
                for i in range(10)]

    def test_a_query_replaces_the_tail_with_the_hits(self, transcript):
        sid = transcript(self.records())
        lines = text_of(preview.render(sid, Snapshot(), "deploy"))
        assert "5 matches" in lines
        assert all("deploy" in l for l in lines if l.startswith("▍"))

    def test_hits_are_highlighted(self, transcript):
        sid = transcript(self.records())
        rows = preview.render(sid, Snapshot(), "deploy")
        hit = [r for r in rows if any(st is Style.HIT for _, st in r)]
        assert hit and all(text == "deploy" for text, st in hit[0] if st is Style.HIT)

    def test_a_query_that_matches_nothing_leaves_the_tail_alone(self, transcript):
        sid = transcript(self.records())
        lines = text_of(preview.render(sid, Snapshot(), "elephant"))
        assert not any(l.startswith("elephant") or "matches" in l for l in lines)
        assert any(l.startswith("▍User nothing here") for l in lines)

    def test_a_parsed_query_works_too(self, transcript):
        sid = transcript(self.records())
        lines = text_of(preview.render(sid, Snapshot(), fuzzy.parse("deploy")))
        assert "5 matches" in lines

    def test_negated_terms_do_not_highlight(self, transcript):
        sid = transcript(self.records())
        rows = preview.render(sid, Snapshot(), "!deploy")
        assert not any(st is Style.HIT for row in rows for _, st in row)

    def test_issue_keys_stay_highlighted(self, transcript):
        sid = transcript([user("ACME-316 and #353")])
        rows = preview.render(sid, Snapshot())
        line = [r for r in rows if plain(r).startswith("▍User")][0]
        styles = {text: st for text, st in line}
        assert styles["ACME-316"] is Style.ISSUE
        assert styles["#353"] is Style.PR


class TestJobFallback:
    def test_a_session_without_a_transcript_shows_its_job(self, fake_home, make_job_state):
        make_job_state(SID, state="working", name="nightly", intent="build",
                       cwd="/repo", createdAt="t0")
        snap = Snapshot(agents={SID: Agent(sid=SID, kind="background", state="working",
                                           live=True)})
        lines = text_of(preview.render(SID, snap))
        assert lines[0] == "nightly"
        assert lines[1] == SID
        assert lines[2] == "bg:working"
        assert "state working" in lines
        assert "intent build" in lines
        assert "cwd /repo" in lines

    def test_the_timeline_tail_is_appended(self, fake_home, make_job_state):
        job = make_job_state(SID, state="done")
        (job.parent / "timeline.jsonl").write_text(
            "\n".join(json.dumps({"step": i}) for i in range(8)) + "\n")
        lines = text_of(preview.render(SID, Snapshot()))
        assert "timeline" in lines
        assert lines[-1] == '{"step": 7}'
        assert sum(1 for l in lines if l.startswith('{"step"')) == 5

    def test_nothing_known_at_all(self, fake_home):
        assert text_of(preview.render(SID, Snapshot())) == [
            f"no transcript and no job state for {SID[:8]}"]

    def test_render_job_alone_returns_nothing_without_state(self, fake_home):
        assert preview.render_job(SID) == []


class TestHugeTranscripts:
    def test_head_and_tail_are_both_read(self, transcript, monkeypatch):
        monkeypatch.setattr(preview, "TAIL", 2048)
        monkeypatch.setattr(preview, "HEAD", 1024)
        records = [{"type": "custom-title", "customTitle": "big one"}]
        records += [user(f"filler {i}" * 20, i) for i in range(200)]
        sid = transcript(records)
        lines = text_of(preview.render(sid, Snapshot()))
        # the title comes from the head chunk, the turns from the tail
        assert lines[0] == "big one"
        assert lines[1].startswith("~")
        assert any(l.startswith("▍User filler 199") for l in lines)

    def test_a_small_transcript_is_not_marked_truncated(self, transcript):
        sid = transcript([user("hi")])
        assert not text_of(preview.render(sid, Snapshot()))[1].startswith("~")


class TestWrap:
    def test_short_rows_pass_through(self):
        rows: list[Row] = [[("hello", Style.NORMAL)]]
        assert preview.wrap(rows, 40) == rows

    def test_a_long_row_is_split_and_marked(self):
        out = preview.wrap([[("abcdefghij", Style.NORMAL)]], 5)
        assert [plain(r) for r in out] == ["abcde", "↳ fgh", "↳ ij"]

    def test_the_continuation_marker_is_dim(self):
        out = preview.wrap([[("abcdefghij", Style.NORMAL)]], 5)
        assert out[1][0] == ("↳ ", Style.DIM)

    def test_styles_survive_the_break(self):
        out = preview.wrap([[("abc", Style.USER), ("defg", Style.HIT)]], 4)
        assert out[0] == [("abc", Style.USER), ("d", Style.HIT)]
        assert out[1] == [("↳ ", Style.DIM), ("ef", Style.HIT)]
        assert out[2] == [("↳ ", Style.DIM), ("g", Style.HIT)]

    def test_wide_characters_do_not_overflow(self):
        out = preview.wrap([[("日本語です", Style.NORMAL)]], 5)
        assert all(sum(2 for _ in plain(r) if True) <= 10 for r in out)
        assert plain(out[0]) == "日本"

    def test_empty_rows_are_kept(self):
        assert preview.wrap([[]], 10) == [[]]

    def test_a_useless_width_leaves_the_rows_alone(self):
        rows: list[Row] = [[("abcdefghij", Style.NORMAL)]]
        assert preview.wrap(rows, 2) == rows


def test_a_broken_transcript_never_raises(transcript, monkeypatch):
    sid = transcript([user("hi")])
    monkeypatch.setattr(preview, "_load", lambda path: (_ for _ in ()).throw(OSError("boom")))
    assert text_of(preview.render(sid, Snapshot())) == ["preview unavailable: OSError"]


def test_unparsable_lines_are_skipped(fake_project, fake_home):
    project_dir = fake_project()
    (project_dir / f"{SID}.jsonl").write_text(
        "not json\n" + json.dumps(user("hi")) + "\n\n{broken\n")
    assert text_of(preview.render(SID, Snapshot()))[0] == "hi"
