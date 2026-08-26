"""The scanner: what it claims, when it publishes, and what it keeps.

The scan runs against the clock the UI polls on, so most of what is worth
testing here is ordering: a change claimed before the work starts, a snapshot
published before the daemon is asked, work kept when a scan is cancelled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cct import daemon, paths, store, transcript
from cct.cache import Cache, cache_path
from cct.store import Loader, Scope

SIDS = [f"{i}{i}{i}{i}{i}{i}{i}{i}-0000-0000-0000-00000000000{i}" for i in range(10)]


def a_project(fake_project, make_transcript, cwd: str, n: int) -> Path:
    project_dir = fake_project(cwd=cwd)
    for i in range(n):
        make_transcript(project_dir, SIDS[i], turns=1)
    return project_dir


def agents_calls(stub_claude) -> list[list[str]]:
    return [argv for argv in stub_claude.calls() if argv[:2] == ["agents", "--json"]]


def bump(path: str | Path, secs: float = 2.0) -> None:
    """Move an mtime far enough that no filesystem's resolution can hide it."""
    st = os.stat(path)
    os.utime(path, (st.st_atime + secs, st.st_mtime + secs))


# -- the fingerprint is claimed where it is computed ----------------------

def test_a_poll_during_a_scan_does_not_ask_for_a_reload(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path, monkeypatch):
    """A scan slower than the poll must not talk itself into a reload."""
    work = tmp_path / "work"
    project_dir = a_project(fake_project, make_transcript, str(work), 4)
    loader = Loader(Scope.project(paths.resolve_dir(str(work))))

    polls = []
    real = transcript.parse_session

    def watched(path, sid=""):
        polls.append(loader.dirty())      # exactly what the UI does twice a second
        if loader.dirty():
            loader.request_reload()
        return real(path, sid)

    monkeypatch.setattr(transcript, "parse_session", watched)
    loader.scan_now()

    assert polls == [False] * 4
    assert len(loader.snapshot().sessions) == 4
    assert Path(cache_path(project_dir.name)).exists()


def test_a_change_that_lands_during_a_scan_is_reported_after_it(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path, monkeypatch):
    work = tmp_path / "work"
    project_dir = a_project(fake_project, make_transcript, str(work), 2)
    loader = Loader(Scope.project(paths.resolve_dir(str(work))))

    real = transcript.parse_session
    landed = []

    def watched(path, sid=""):
        if not landed:
            landed.append(make_transcript(project_dir, SIDS[9], turns=1))
        return real(path, sid)

    monkeypatch.setattr(transcript, "parse_session", watched)
    loader.scan_now()

    assert loader.dirty()


# -- publish order --------------------------------------------------------

def spy_publishes(monkeypatch) -> list[tuple[bool, int, int]]:
    """``(scanning, sessions, agents)`` for every publish, in order."""
    out: list[tuple[bool, int, int]] = []
    real = Loader._publish

    def spy(self, sessions, agents, *rest, **kw):
        out.append((kw["scanning"], len(sessions), len(agents)))
        return real(self, sessions, agents, *rest, **kw)

    monkeypatch.setattr(Loader, "_publish", spy)
    return out


def test_the_transcript_side_is_published_before_the_agents_view(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path, monkeypatch):
    work = tmp_path / "work"
    a_project(fake_project, make_transcript, str(work), 4)
    stub_claude.set_agents([{"sessionId": SIDS[0], "kind": "background",
                             "state": "done", "cwd": str(work)}])
    publishes = spy_publishes(monkeypatch)

    loader = Loader(Scope.project(paths.resolve_dir(str(work))))
    loader.scan_now()

    assert publishes == [(True, 4, 0), (False, 4, 1)]
    assert len(loader.snapshot().agents) == 1


def test_no_publish_shows_a_part_of_the_list(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path, monkeypatch):
    """A scan shorter than the publish interval publishes the list once, whole."""
    work = tmp_path / "work"
    a_project(fake_project, make_transcript, str(work), 8)
    publishes = spy_publishes(monkeypatch)

    Loader(Scope.project(paths.resolve_dir(str(work)))).scan_now()

    assert [n for scanning, n, _ in publishes if scanning] == [8]


# -- the agents view ------------------------------------------------------

def test_the_agents_view_is_always_asked_for_every_project(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path):
    work = tmp_path / "work"
    a_project(fake_project, make_transcript, str(work), 1)
    abs_dir = paths.resolve_dir(str(work))

    store.scan_once(Scope.project(abs_dir))

    assert agents_calls(stub_claude) == [["agents", "--json", "--all", "--cwd", abs_dir]]


def test_a_row_whose_fields_are_not_strings_still_yields_an_agent(fake_home, stub_claude):
    """The UI hashes state and status, so nothing but a string may reach it."""
    stub_claude.set_agents([
        "not a row",
        {"kind": "background", "state": "done"},                  # no session id
        {"sessionId": SIDS[0], "kind": "background",
         "state": {"phase": "working"}, "status": ["idle"], "name": None},
    ])

    agents = daemon.list_agents()

    assert [a.sid for a in agents] == [SIDS[0]]
    assert (agents[0].state, agents[0].status, agents[0].name) == ("", "", "")


def test_the_agents_view_is_skipped_while_the_daemon_has_not_moved(
        fake_home, fake_project, make_transcript, make_job_state, stub_claude,
        tmp_path):
    work = tmp_path / "work"
    a_project(fake_project, make_transcript, str(work), 2)
    state = make_job_state(SIDS[1], state="working")
    stub_claude.set_agents([{"sessionId": SIDS[0], "kind": "background",
                             "state": "done", "cwd": str(work)}])

    loader = Loader(Scope.project(paths.resolve_dir(str(work))))
    for _ in range(3):
        loader.scan_now()

    assert len(agents_calls(stub_claude)) == 1
    assert len(loader.snapshot().agents) == 1      # reused, not lost

    bump(state)                                    # a job rewrote its state
    loader.scan_now()

    assert len(agents_calls(stub_claude)) == 2
    assert len(loader.snapshot().agents) == 1


def test_an_empty_agents_view_is_asked_again(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path):
    """An empty answer may be a daemon that was down, so it is never cached."""
    work = tmp_path / "work"
    a_project(fake_project, make_transcript, str(work), 1)

    loader = Loader(Scope.project(paths.resolve_dir(str(work))))
    loader.scan_now()
    loader.scan_now()

    assert len(agents_calls(stub_claude)) == 2


def test_the_state_fingerprint_moves_with_everything_the_agents_view_reads(
        fake_home, make_job_state):
    before = daemon.state_fingerprint()
    state = make_job_state(SIDS[0], state="working")
    after = daemon.state_fingerprint()
    assert after != before

    bump(state)                                  # the job rewrote its state
    rewritten = daemon.state_fingerprint()
    assert rewritten != after
    # the jobs directory's own mtime does not move for that, which is why every
    # state file has to be statted
    assert rewritten[1] == after[1]
    assert rewritten[2] != after[2]

    bump(paths.ROSTER)                           # a worker came or went
    assert daemon.state_fingerprint() != rewritten

    roster_moved = daemon.state_fingerprint()
    os.unlink(state)
    os.rmdir(os.path.dirname(state))             # the job was removed
    assert daemon.state_fingerprint() != roster_moved


# -- change detection -----------------------------------------------------

def test_an_append_is_noticed_in_every_project_scope(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path):
    one = a_project(fake_project, make_transcript, str(tmp_path / "one"), 2)
    a_project(fake_project, make_transcript, str(tmp_path / "two"), 2)

    loader = Loader(Scope.everything(str(tmp_path / "one")))
    loader.scan_now()
    assert not loader.dirty()

    target = one / f"{SIDS[0]}.jsonl"
    with open(target, "a") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "extra",
                             "message": {"content": "one more"}}) + "\n")
    bump(target)

    assert loader.dirty()


def test_the_fingerprint_counts_every_transcript_past_the_stat_limit(
        fake_home, fake_project, make_transcript, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FILE_STAT_LIMIT", 3)
    project_dir = a_project(fake_project, make_transcript, str(tmp_path / "work"), 5)

    count, newest = store._dir_fingerprint(str(project_dir))
    assert count == 5           # the real count, not the limit plus one
    assert newest == 0          # past the limit, appends are not noticed

    make_transcript(project_dir, SIDS[5], turns=1)
    assert store._dir_fingerprint(str(project_dir))[0] == 6


def test_under_the_stat_limit_the_newest_mtime_is_part_of_it(
        fake_home, fake_project, make_transcript, tmp_path):
    project_dir = a_project(fake_project, make_transcript, str(tmp_path / "work"), 2)

    count, newest = store._dir_fingerprint(str(project_dir))
    assert count == 2 and newest > 0

    bump(project_dir / f"{SIDS[0]}.jsonl")
    assert store._dir_fingerprint(str(project_dir)) != (count, newest)


# -- cancellation ---------------------------------------------------------

def test_a_cancelled_scan_keeps_the_directories_it_finished(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path, monkeypatch):
    first = a_project(fake_project, make_transcript, str(tmp_path / "aaa"), 1)
    second = a_project(fake_project, make_transcript, str(tmp_path / "bbb"), 2)
    assert sorted([str(first), str(second)]) == [str(first), str(second)]

    loader = Loader(Scope.everything(str(tmp_path / "aaa")))
    real = transcript.parse_session
    parsed = []

    def watched(path, sid=""):
        parsed.append(sid)
        if len(parsed) == 2:      # halfway through the second directory
            loader.request_reload()
        return real(path, sid)

    monkeypatch.setattr(transcript, "parse_session", watched)
    loader.scan_now()

    assert Path(cache_path(first.name)).exists()
    assert not Path(cache_path(second.name)).exists()
    with open(cache_path(first.name)) as fh:
        assert set(json.load(fh)) == {SIDS[0]}


# -- paths primitives -----------------------------------------------------

def test_find_transcript_takes_the_newest_copy(
        fake_home, fake_project, make_transcript, tmp_path):
    older = a_project(fake_project, make_transcript, str(tmp_path / "one"), 1)
    newer = fake_project(cwd=str(tmp_path / "two"))
    make_transcript(newer, SIDS[0], turns=1)
    bump(newer / f"{SIDS[0]}.jsonl", 60.0)

    assert paths.find_transcript(SIDS[0]) == str(newer / f"{SIDS[0]}.jsonl")
    assert os.path.dirname(paths.find_transcript(SIDS[0])) != str(older)


def test_find_transcript_is_empty_when_no_project_holds_it(fake_home):
    assert paths.find_transcript(SIDS[0]) == ""


def test_encoded_of_reads_a_project_directory_however_it_is_spelled():
    assert paths.encoded_of("/a/b/-tmp-work") == "-tmp-work"
    assert paths.encoded_of("/a/b/-tmp-work/") == "-tmp-work"


def test_a_scope_builds_its_project_directory_from_the_shared_primitive(fake_home):
    scope = Scope.project("/tmp/work")
    assert scope.project_dir == paths.project_dir_for("/tmp/work")
    assert Scope.everything().project_dir == ""


# -- the cache is left intact by a cancelled scan -------------------------

def test_a_kept_cache_is_read_back_by_the_next_scan(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path):
    project_dir = a_project(fake_project, make_transcript, str(tmp_path / "work"), 2)
    scope = Scope.project(paths.resolve_dir(str(tmp_path / "work")))
    store.scan_once(scope)

    warm = Cache(project_dir.name)
    path = str(project_dir / f"{SIDS[0]}.jsonl")
    assert warm.get(SIDS[0], path, os.stat(path)) is not None
    assert warm.hits == 1
