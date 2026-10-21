"""Transcript parsing: what a session record set says about the session."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cct import transcript
from cct.transcript import HAY_ASST, HAY_MSG, HAY_USER

SID = "11111111-2222-3333-4444-555555555555"


def write(tmp_path: Path, records: list, sid: str = SID) -> str:
    path = tmp_path / f"{sid}.jsonl"
    with open(path, "w") as fh:
        for record in records:
            fh.write(record if isinstance(record, str) else json.dumps(record))
            fh.write("\n")
    return str(path)


def user(text: str, **extra) -> dict:
    return {"type": "user", "message": {"content": text}, **extra}


def assistant(text: str = "", **extra) -> dict:
    return {"type": "assistant", "message": {"content": text}, **extra}


def test_sid_and_paths_come_from_the_filename(tmp_path):
    session = transcript.parse_session(write(tmp_path, [user("hi")]))
    assert session.sid == SID
    assert session.project_dir == str(tmp_path)
    assert session.path.endswith(f"{SID}.jsonl")


def test_title_precedence(tmp_path):
    records = [user("first thing said"), {"type": "ai-title", "aiTitle": "guessed"}]
    assert transcript.parse_session(write(tmp_path, records)).title == "guessed"
    records.append({"type": "custom-title", "customTitle": "chosen"})
    assert transcript.parse_session(write(tmp_path, records)).title == "chosen"


def test_title_falls_back_to_first_message_then_to_the_short_id(tmp_path):
    assert transcript.parse_session(write(tmp_path, [user("first thing said")])).title \
        == "first thing said"
    assert transcript.parse_session(write(tmp_path, [assistant("only me")])).title == SID[:8]


def test_the_last_title_record_wins(tmp_path):
    records = [{"type": "custom-title", "customTitle": "old"},
               {"type": "custom-title", "customTitle": "new"},
               {"type": "custom-title", "customTitle": "   "}]
    assert transcript.parse_session(write(tmp_path, records)).custom == "new"


def test_user_turns_exclude_meta_sidechain_and_tag_bodies(tmp_path):
    records = [
        user("real one"),
        user("meta", isMeta=True),
        user("side", isSidechain=True),
        user("<command-name>/foo</command-name>"),
        user("real two"),
        assistant("not a user turn"),
    ]
    session = transcript.parse_session(write(tmp_path, records))
    assert session.users == 2
    assert session.count == 6
    assert session.first == "real one"


def test_context_is_the_last_main_chain_usage(tmp_path):
    usage = {"input_tokens": 10, "cache_read_input_tokens": 100,
             "cache_creation_input_tokens": 1000, "output_tokens": 7}
    records = [
        {"type": "assistant", "message": {"content": "a", "usage": usage}},
        {"type": "assistant", "message": {"content": "b", "usage": {"input_tokens": 5}},
         "isSidechain": True},
        {"type": "assistant", "message": {"content": "c",
                                          "usage": {"input_tokens": 2, "output_tokens": 9}}},
    ]
    assert transcript.parse_session(write(tmp_path, records)).ctx == 2


def test_context_is_none_without_usage(tmp_path):
    assert transcript.parse_session(write(tmp_path, [user("hi"), assistant("ho")])).ctx is None


def test_a_transcript_without_turns_is_a_stub(tmp_path):
    records = [{"type": "custom-title", "customTitle": "named but empty"},
               {"type": "summary", "summary": "nothing"}]
    session = transcript.parse_session(write(tmp_path, records))
    assert session.stub is True
    assert session.count == 0
    assert transcript.parse_session(write(tmp_path, [user("hi")])).stub is False


def test_branch_and_cwd_take_the_last_non_empty_value(tmp_path):
    records = [
        user("a", gitBranch="main", cwd="/tmp/one"),
        assistant("b", gitBranch="", cwd=""),
        assistant("c", gitBranch="feat/x", cwd="/tmp/two"),
        {"type": "summary", "gitBranch": "ignored", "cwd": "/ignored"},
    ]
    session = transcript.parse_session(write(tmp_path, records))
    assert session.branch == "feat/x"
    assert session.cwd == "/tmp/two"


def test_uuids_are_collected_in_file_order_from_turns_only(tmp_path):
    records = [user("a", uuid="u1"), {"type": "summary", "uuid": "skip"},
               assistant("b", uuid="u2"), user("c")]
    assert transcript.parse_session(write(tmp_path, records)).uuids == ["u1", "u2"]


def test_timestamps_bracket_the_session(tmp_path):
    records = [user("a", timestamp="2026-01-02T10:00:00.000Z"),
               assistant("b", timestamp="2026-01-01T09:00:00.000Z"),
               assistant("c", timestamp="not a date"),
               assistant("d", timestamp="2026-01-03T11:30:00.000Z")]
    session = transcript.parse_session(write(tmp_path, records))
    assert session.ts_min == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert session.ts_max == datetime(2026, 1, 3, 11, 30, tzinfo=timezone.utc)


def test_links_are_deduplicated(tmp_path):
    records = [
        {"type": "pr-link", "prNumber": 353},
        {"type": "pr-link", "prNumber": "142"},
        {"type": "pr-link", "prNumber": 353},
        {"type": "pr-link", "prNumber": None},
        {"type": "frame-link", "frameUrl": "https://a"},
        {"type": "frame-link", "frameUrl": "https://a"},
        {"type": "frame-link", "frameUrl": "https://b"},
        {"type": "frame-link", "frameUrl": ""},
    ]
    session = transcript.parse_session(write(tmp_path, records))
    assert session.prs == {353, 142}
    assert session.frames == 2


def test_worktree_name_wins_over_the_relocated_directory(tmp_path):
    relocated = {"type": "relocated", "relocatedCwd": "/home/user/thing/wt-a/"}
    assert transcript.parse_session(write(tmp_path, [relocated])).worktree == "wt-a"
    named = {"type": "worktree-state", "worktreeSession": {"worktreeName": "pr420-review"}}
    assert transcript.parse_session(write(tmp_path, [relocated, named])).worktree \
        == "pr420-review"


def test_worktree_state_names_the_repo_root_it_started_from(tmp_path):
    state = {"type": "worktree-state",
             "worktreeSession": {"originalCwd": "/home/user/thing",
                                 "worktreePath": "/home/user/thing/.claude/worktrees/wt-a",
                                 "worktreeName": "wt-a"}}
    assert transcript.parse_session(write(tmp_path, [state])).origin == "/home/user/thing"
    assert transcript.parse_session(write(tmp_path, [user("hi")])).origin == ""


def test_content_blocks_flatten_to_their_text(tmp_path):
    content = [{"type": "text", "text": "hello"},
               {"type": "tool_use", "name": "Bash"},
               {"type": "text", "text": "world"}]
    session = transcript.parse_session(write(tmp_path, [{"type": "user",
                                                        "message": {"content": content}}]))
    assert session.first == "hello world"


def test_haystack_respects_the_per_message_and_total_budgets(tmp_path):
    records = [user("u" * (HAY_MSG + 50)) for _ in range(40)]
    records += [assistant("a" * (HAY_MSG + 50)) for _ in range(40)]
    session = transcript.parse_session(write(tmp_path, records))
    assert "u" * HAY_MSG in session.hay
    assert "u" * (HAY_MSG + 1) not in session.hay
    users = session.hay.count("u" * HAY_MSG)
    assistants = session.hay.count("a" * HAY_MSG)
    assert users == HAY_USER // (HAY_MSG + 1) + 1
    assert assistants == HAY_ASST // (HAY_MSG + 1) + 1


def test_unparsable_lines_are_skipped(tmp_path):
    records = ["{not json", "", "[1, 2, 3]", json.dumps(user("survivor"))]
    session = transcript.parse_session(write(tmp_path, records))
    assert session.users == 1
    assert session.first == "survivor"


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        transcript.parse_session(str(tmp_path / "nope.jsonl"))
