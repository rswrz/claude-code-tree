"""Parent resolution, forest ordering, and the scan that feeds them."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cct import paths, tree
from cct.model import Session, Sort
from cct.store import Scope, scan_once

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def sess(sid: str, uuids=(), mtime: float = 0.0, **kw) -> Session:
    return Session(sid=sid, uuids=list(uuids), mtime=mtime, **kw)


def forest(sessions, jobs=None, roster=None, sort=Sort.RECENT):
    by_sid = {s.sid: s for s in sessions}
    return tree.build_forest(by_sid, jobs or {}, roster or {}, sort)


def skeleton(roots, children, by_sid=None) -> list[str]:
    return [row.prefix + row.connector + row.sid for row in tree.walk(roots, children)]


# -- parent resolution ---------------------------------------------------

def test_a_shared_uuid_prefix_makes_a_child():
    parent = sess("p", ["a", "b", "c"], mtime=1)
    child = sess("c", ["a", "b", "c", "d"], mtime=2)
    stranger = sess("s", ["x", "y"], mtime=3)
    _, _, parents = forest([parent, child, stranger])
    assert parents == {"p": None, "c": "p", "s": None}


def test_the_longest_prefix_wins_and_ties_go_to_the_oldest():
    near = sess("near", ["a", "b"], mtime=1)
    nearer = sess("nearer", ["a", "b", "c"], mtime=2)
    also_near = sess("also", ["a", "b"], mtime=3)
    child = sess("child", ["a", "b", "c", "d"], mtime=4)
    _, _, parents = forest([near, nearer, also_near, child])
    assert parents["child"] == "nearer"
    assert parents["also"] == "near"


def test_only_earlier_sessions_can_be_parents():
    younger = sess("younger", ["a", "b", "c"], mtime=9)
    older = sess("older", ["a", "b"], mtime=1)
    _, _, parents = forest([younger, older])
    assert parents == {"older": None, "younger": "older"}


def test_the_roster_fork_parent_outranks_the_uuid_prefix():
    inferred = sess("inferred", ["a", "b"], mtime=1)
    explicit = sess("explicit", ["z"], mtime=2)
    child = sess("child", ["a", "b", "c"], mtime=3)
    _, _, parents = forest([inferred, explicit, child], roster={"child": "explicit"})
    assert parents["child"] == "explicit"


def test_the_job_fork_parent_outranks_the_uuid_prefix_but_not_the_roster():
    inferred = sess("inferred", ["a", "b"], mtime=1)
    from_job = sess("job", ["z"], mtime=2)
    from_roster = sess("roster", ["y"], mtime=2)
    child = sess("child", ["a", "b", "c"], mtime=3)
    everyone = [inferred, from_job, from_roster, child]
    jobs = {"child": {"forkParentSessionId": "job"}}
    _, _, parents = forest(everyone, jobs=jobs)
    assert parents["child"] == "job"
    _, _, parents = forest(everyone, jobs=jobs, roster={"child": "roster"})
    assert parents["child"] == "roster"


def test_a_parent_outside_the_scope_is_ignored():
    child = sess("child", ["a"], mtime=1)
    _, _, parents = forest([child], roster={"child": "not-here"},
                           jobs={"child": {"forkParentSessionId": "also-not-here"}})
    assert parents["child"] is None


def test_a_turnless_session_is_adopted_by_its_one_namesake():
    named = sess("named", ["a"], mtime=1, count=2, custom="Fix the thing")
    ghost = sess("ghost", mtime=2)
    jobs = {"ghost": {"name": "Fix the thing"}}
    _, _, parents = forest([named, ghost], jobs=jobs)
    assert parents["ghost"] == "named"


def test_an_ambiguous_namesake_adopts_nobody():
    one = sess("one", ["a"], mtime=1, count=2, custom="Fix the thing")
    two = sess("two", ["b"], mtime=1, count=2, ai="Fix the thing")
    ghost = sess("ghost", mtime=2)
    _, _, parents = forest([one, two, ghost], jobs={"ghost": {"name": "Fix the thing"}})
    assert parents["ghost"] is None


def test_a_session_with_turns_is_never_adopted_by_name():
    named = sess("named", ["a"], mtime=1, count=2, custom="Fix the thing")
    other = sess("other", ["z"], mtime=2, count=2)
    _, _, parents = forest([named, other], jobs={"other": {"name": "Fix the thing"}})
    assert parents["other"] is None


def test_a_session_is_never_its_own_parent():
    only = sess("only", ["a"], mtime=1)
    _, _, parents = forest([only], roster={"only": "only"})
    assert parents["only"] is None


def test_a_cycle_is_broken_into_a_chain():
    a = sess("a", ["x"], mtime=1)
    b = sess("b", ["y"], mtime=2)
    roots, children, parents = forest([a, b], roster={"a": "b", "b": "a"})
    assert roots == ["a"]
    assert children["a"] == ["b"]
    assert parents["a"] is None


# -- ordering ------------------------------------------------------------

def test_touching_a_leaf_lifts_its_whole_tree():
    old_root = sess("old-root", ["a"], mtime=10)
    fresh_leaf = sess("leaf", ["a", "b"], mtime=100)
    lone = sess("lone", ["z"], mtime=50)
    roots, children, _ = forest([old_root, fresh_leaf, lone])
    assert roots == ["old-root", "lone"]
    assert children["old-root"] == ["leaf"]


def test_siblings_share_the_subtree_ranking():
    root = sess("root", ["a"], mtime=1)
    quiet = sess("quiet", ["a", "q"], mtime=2)
    busy = sess("busy", ["a", "b"], mtime=3)
    grandchild = sess("grand", ["a", "q", "g"], mtime=99)
    roots, children, _ = forest([root, quiet, busy, grandchild])
    assert roots == ["root"]
    assert children["root"] == ["quiet", "busy"]


def test_a_sid_missing_from_the_snapshot_contributes_no_recency():
    root = sess("root", ["a"], mtime=10)
    recent = tree.subtree_mtime({"root": root}, {"root": ["gone"]}, ["root", "ghost"])
    assert recent == {"root": 10.0, "gone": 0.0, "ghost": 0.0}


def test_sorting_reorders_but_never_flattens():
    root = sess("root", ["a"], mtime=1, users=1)
    child = sess("child", ["a", "b"], mtime=2, users=50)
    lone = sess("lone", ["z"], mtime=3, users=10)
    roots, children, _ = forest([root, child, lone], sort=Sort.TURNS)
    assert roots == ["lone", "root"]
    assert children["root"] == ["child"]


def test_name_sort_is_ascending_and_case_insensitive():
    a = sess("a", ["1"], mtime=1, custom="beta")
    b = sess("b", ["2"], mtime=2, custom="Alpha")
    c = sess("c", ["3"], mtime=3, custom="gamma")
    roots, _, _ = forest([a, b, c], sort=Sort.NAME)
    assert roots == ["b", "a", "c"]


def test_context_and_duration_sorts_rank_high_first():
    a = sess("a", ["1"], mtime=1, ctx=10, ts_min=T0, ts_max=T0 + timedelta(hours=5))
    b = sess("b", ["2"], mtime=2, ctx=900, ts_min=T0, ts_max=T0 + timedelta(minutes=1))
    c = sess("c", ["3"], mtime=3)
    assert forest([a, b, c], sort=Sort.CONTEXT)[0] == ["b", "a", "c"]
    assert forest([a, b, c], sort=Sort.DURATION)[0] == ["a", "b", "c"]


# -- display order -------------------------------------------------------

def test_walk_draws_the_skeleton():
    roots = ["r1", "r2"]
    children = {"r1": ["c1", "c2"], "c1": ["g1"], "c2": [], "g1": [], "r2": []}
    assert skeleton(roots, children) == [
        "├─ r1",
        "│  ├─ c1",
        "│  │  └─ g1",
        "│  └─ c2",
        "└─ r2",
    ]


def test_walk_visits_every_session_once():
    sessions = [sess("root", ["a"], mtime=1)]
    sessions += [sess(f"kid{i}", ["a", str(i)], mtime=i + 2) for i in range(5)]
    roots, children, _ = forest(sessions)
    rows = tree.walk(roots, children)
    assert len(rows) == len(sessions)
    assert {row.sid for row in rows} == {s.sid for s in sessions}
    assert [row.depth for row in rows] == [0, 1, 1, 1, 1, 1]


# -- the scan that feeds the forest --------------------------------------

def test_a_scan_finds_the_sessions_of_one_project(fake_home, fake_project,
                                                  make_transcript, stub_claude, tmp_path):
    work = tmp_path / "work"
    project_dir = fake_project(cwd=str(work))
    make_transcript(project_dir, "aaaaaaaa-0000-0000-0000-000000000001", turns=2)
    make_transcript(project_dir, "bbbbbbbb-0000-0000-0000-000000000002", turns=1)
    snap = scan_once(Scope.project(paths.resolve_dir(str(work))), use_cache=False)
    assert len(snap.sessions) == 2
    assert snap.scanning is False and snap.scanned == snap.total == 2
    assert sorted(snap.roots) == sorted(snap.sessions)
    assert all(s.cwd for s in snap.sessions.values())


def test_a_forked_transcript_scans_into_a_child(fake_home, fake_project,
                                                make_transcript, stub_claude, tmp_path):
    work = tmp_path / "work"
    project_dir = fake_project(cwd=str(work))
    parent_sid = "aaaaaaaa-0000-0000-0000-000000000001"
    parent_path = make_transcript(project_dir, parent_sid, turns=3)
    lines = parent_path.read_text().splitlines()
    child_sid = "bbbbbbbb-0000-0000-0000-000000000002"
    extra = json.dumps({"type": "user", "uuid": "own-1", "cwd": str(work),
                        "message": {"content": "carry on"}})
    (project_dir / f"{child_sid}.jsonl").write_text("\n".join(lines[:4] + [extra]) + "\n")
    snap = scan_once(Scope.project(paths.resolve_dir(str(work))), use_cache=False)
    assert snap.parents[child_sid] == parent_sid
    assert snap.roots == [parent_sid]


def test_a_background_session_with_no_transcript_becomes_a_ghost(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path):
    work = tmp_path / "work"
    project_dir = fake_project(cwd=str(work))
    make_transcript(project_dir, "aaaaaaaa-0000-0000-0000-000000000001", turns=1)
    ghost_sid = "cccccccc-0000-0000-0000-000000000003"
    stub_claude.set_agents([{"id": ghost_sid[:8], "sessionId": ghost_sid,
                             "kind": "background", "state": "blocked",
                             "name": "the missing job", "cwd": str(work),
                             "startedAt": 1787719953932}])
    snap = scan_once(Scope.project(paths.resolve_dir(str(work))), use_cache=False)
    ghost = snap.sessions[ghost_sid]
    assert ghost.ghost is True
    assert ghost.title == "the missing job"
    assert ghost.mtime == 1787719953.932
    assert snap.agents[ghost_sid].state == "blocked"


def test_a_background_session_living_in_a_worktree_is_labelled_by_it(
        fake_home, fake_project, make_transcript, stub_claude, tmp_path):
    work = tmp_path / "work"
    project_dir = fake_project(cwd=str(work))
    make_transcript(project_dir, "aaaaaaaa-0000-0000-0000-000000000001", turns=1)
    elsewhere = Path(paths.PROJECTS_DIR) / (project_dir.name + "--claude-worktrees-fix-it")
    elsewhere.mkdir()
    away_sid = "dddddddd-0000-0000-0000-000000000004"
    make_transcript(elsewhere, away_sid, turns=1)
    stub_claude.set_agents([{"sessionId": away_sid, "kind": "background",
                             "state": "done", "name": "away", "cwd": str(work)}])
    snap = scan_once(Scope.project(paths.resolve_dir(str(work))), use_cache=False)
    away = snap.sessions[away_sid]
    assert away.ghost is False
    assert away.worktree == "claude-worktrees-fix-it"


def test_all_projects_scope_gathers_every_directory(fake_home, fake_project,
                                                    make_transcript, stub_claude, tmp_path):
    one = fake_project(cwd=str(tmp_path / "one"))
    two = fake_project(cwd=str(tmp_path / "two"))
    make_transcript(one, "aaaaaaaa-0000-0000-0000-000000000001", turns=1,
                    cwd=str(tmp_path / "one"))
    make_transcript(two, "bbbbbbbb-0000-0000-0000-000000000002", turns=1,
                    cwd=str(tmp_path / "two"))
    snap = scan_once(Scope.everything(str(tmp_path / "one")), use_cache=False)
    assert len(snap.sessions) == 2
    labels = {p.label for p in snap.projects.values()}
    assert labels == {"one", "two"}
    assert snap.default_branch == ""


def test_the_job_fork_parent_survives_a_scan(fake_home, fake_project, make_transcript,
                                             make_job_state, stub_claude, tmp_path):
    work = tmp_path / "work"
    project_dir = fake_project(cwd=str(work))
    parent_sid = "aaaaaaaa-0000-0000-0000-000000000001"
    child_sid = "bbbbbbbb-0000-0000-0000-000000000002"
    make_transcript(project_dir, parent_sid, turns=1)
    make_transcript(project_dir, child_sid, turns=1)
    make_job_state(child_sid, forkParentSessionId=parent_sid)
    snap = scan_once(Scope.project(paths.resolve_dir(str(work))), use_cache=False)
    assert snap.parents[child_sid] == parent_sid
    assert snap.jobs[child_sid]["state"] == "done"
