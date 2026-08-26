"""Partitioning the ordered root list into labelled blocks."""

from __future__ import annotations

from cct.group import AGE_LABELS, partition
from cct.model import Group, Project, Session, Snapshot

NOW = 1_000_000.0
DAY = 86400.0


def sess(sid: str, mtime: float = NOW, cwd: str = "") -> Session:
    return Session(sid=sid, mtime=mtime, cwd=cwd)


def proj(cwd: str, label: str = "", encoded: str = "") -> Project:
    return Project(encoded=encoded or cwd.replace("/", "-"), path="/p" + cwd,
                   cwd=cwd, label=label)


def snapshot(*sessions: Session, projects=None) -> Snapshot:
    return Snapshot(sessions={s.sid: s for s in sessions}, projects=projects or {})


def flat(*sids: str) -> dict[str, list[str]]:
    return {sid: [] for sid in sids}


def blocks_of(snap: Snapshot, roots, children, mode, now=NOW):
    return [(b.key, b.label, b.roots) for b in partition(snap, roots, children, mode, now)]


# -- no grouping ---------------------------------------------------------

def test_without_grouping_everything_lands_in_one_unlabelled_block():
    snap = snapshot(sess("a"), sess("b"))
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.NONE) == [
        ("", "", ["a", "b"]),
    ]


# -- by project ----------------------------------------------------------

def test_every_worktree_of_one_repo_shares_its_block():
    projects = {
        "a": proj("/repo/app/.claude/worktrees/fix", label="fix"),
        "b": proj("/repo/app/.claude/worktrees/feat", label="feat"),
        "c": proj("/repo/app", label="app"),
    }
    snap = snapshot(sess("a"), sess("b"), sess("c"), projects=projects)
    roots = ["a", "b", "c"]
    assert blocks_of(snap, roots, flat(*roots), Group.PROJECT) == [
        ("/repo/app", "app", ["a", "b", "c"]),
    ]


def test_a_session_groups_by_the_cwd_it_names_itself():
    shared = proj("/repo/app")
    projects = {"a": shared, "b": shared}
    snap = snapshot(sess("a", cwd="/repo/app"), sess("b", cwd="/other/lib"),
                    projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/repo/app", "app", ["a"]),
        ("/other/lib", "lib", ["b"]),
    ]


def test_one_cwd_under_two_project_directories_is_one_block():
    projects = {"a": proj("/repo/app"), "b": proj("/repo/app/sub")}
    snap = snapshot(sess("a", cwd="/repo/app/sub"), sess("b", cwd="/repo/app/sub"),
                    projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/repo/app/sub", "sub", ["a", "b"]),
    ]


def test_a_session_naming_no_cwd_takes_the_one_its_project_directory_names():
    projects = {"a": proj("/repo/app")}
    snap = snapshot(sess("a"), projects=projects)
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [
        ("/repo/app", "app", ["a"]),
    ]


def test_a_worktree_block_is_labelled_by_its_repo():
    projects = {"a": proj("/repo/app/.claude/worktrees/fix", label="fix")}
    snap = snapshot(sess("a"), projects=projects)
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [
        ("/repo/app", "app", ["a"]),
    ]


def test_a_directory_below_a_worktree_folds_into_the_repo():
    projects = {"a": proj("/repo/app/.claude/worktrees/fix/sub", label="sub"),
                "b": proj("/repo/app", label="app")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/repo/app", "app", ["a", "b"]),
    ]


def test_a_worktree_named_with_a_slash_folds_into_the_repo():
    projects = {"a": proj("/repo/app/.claude/worktrees/feature/fix", label="fix")}
    snap = snapshot(sess("a"), projects=projects)
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [
        ("/repo/app", "app", ["a"]),
    ]


def test_a_worktree_of_a_worktree_folds_into_the_outermost_repo():
    cwd = "/repo/app/.claude/worktrees/one/.claude/worktrees/two"
    projects = {"a": proj(cwd, label="two")}
    snap = snapshot(sess("a"), projects=projects)
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [
        ("/repo/app", "app", ["a"]),
    ]


def test_a_project_at_the_filesystem_root_keeps_its_own_labelled_block():
    projects = {"a": proj("/"), "b": proj("/repo/x", label="x")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/", "/", ["a"]),
        ("/repo/x", "x", ["b"]),
    ]


def test_the_root_project_and_the_unknown_bucket_stay_apart_in_either_order():
    snap = snapshot(sess("a"), sess("b"), projects={"a": proj("/")})
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/", "/", ["a"]),
        ("", "(unknown)", ["b"]),
    ]
    assert blocks_of(snap, ["b", "a"], flat("a", "b"), Group.PROJECT) == [
        ("", "(unknown)", ["b"]),
        ("/", "/", ["a"]),
    ]


def test_a_worktree_of_the_root_project_folds_into_it():
    projects = {"a": proj("/.claude/worktrees/fix", label="fix")}
    snap = snapshot(sess("a"), projects=projects)
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [("/", "/", ["a"])]


def test_two_repos_of_the_same_name_stay_apart():
    projects = {"a": proj("/one/app", label="app"), "b": proj("/two/app", label="app")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/one/app", "one/app", ["a"]),
        ("/two/app", "two/app", ["b"]),
    ]


def test_a_project_without_a_cwd_joins_the_repo_its_name_encodes():
    projects = {"a": proj("/repo/app"),
                "b": Project(encoded="-repo-app", path="/p/-repo-app", label="app")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/repo/app", "app", ["a", "b"]),
    ]


def test_a_dash_in_a_repo_name_does_not_split_it_off_a_second_block():
    encoded = "-w-HEON-Administration-exchange-management"
    projects = {"a": proj("/w/HEON-Administration/exchange-management"),
                "b": Project(encoded=encoded, path="/p/" + encoded, label="management")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/w/HEON-Administration/exchange-management", "exchange-management", ["a", "b"]),
    ]


def test_a_project_naming_no_repo_is_headed_by_its_own_directory():
    # the label the store decoded, 'management', is one reading of the dashes
    # among many, so the header says only what the directory is called
    encoded = "-w-HEON-Administration-exchange-management"
    projects = {"a": Project(encoded=encoded, path="/p/" + encoded, label="management")}
    snap = snapshot(sess("a"), projects=projects)
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [
        (encoded, encoded, ["a"]),
    ]


def test_two_repos_encoding_alike_leave_the_encoded_project_on_its_own():
    projects = {"a": proj("/one/a-b"), "b": proj("/one/a/b"),
                "c": Project(encoded="-one-a-b", path="/p/-one-a-b", label="b")}
    snap = snapshot(sess("a"), sess("b"), sess("c"), projects=projects)
    roots = ["a", "b", "c"]
    assert blocks_of(snap, roots, flat(*roots), Group.PROJECT) == [
        ("/one/a-b", "a-b", ["a"]),
        ("/one/a/b", "b", ["b"]),
        ("-one-a-b", "-one-a-b", ["c"]),
    ]


def test_a_session_without_a_project_is_unknown():
    snap = snapshot(sess("a"))
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [("", "(unknown)", ["a"])]


def test_a_ghost_session_joins_the_repo_it_ran_in():
    # the daemon knows where it ran, though no project directory holds it
    ghost = Session(sid="b", cwd="/repo/app", ghost=True)
    snap = snapshot(sess("a"), ghost, projects={"a": proj("/repo/app")})
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/repo/app", "app", ["a", "b"]),
    ]


def test_a_ghost_session_in_a_worktree_joins_its_repo():
    ghost = Session(sid="a", cwd="/repo/app/.claude/worktrees/fix", ghost=True)
    snap = snapshot(ghost)
    assert blocks_of(snap, ["a"], flat("a"), Group.PROJECT) == [
        ("/repo/app", "app", ["a"]),
    ]


# -- colliding labels ----------------------------------------------------

def test_a_collision_grows_by_the_segments_it_takes():
    projects = {"a": proj("/x/team/app"), "b": proj("/y/team/app")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert [label for _, label, _ in
            blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT)] == [
        "x/team/app", "y/team/app",
    ]


def test_a_unique_label_beside_a_collision_stays_short():
    projects = {"a": proj("/one/app"), "b": proj("/two/app"), "c": proj("/one/lib")}
    snap = snapshot(sess("a"), sess("b"), sess("c"), projects=projects)
    roots = ["a", "b", "c"]
    assert [label for _, label, _ in
            blocks_of(snap, roots, flat(*roots), Group.PROJECT)] == [
        "one/app", "two/app", "lib",
    ]


def test_three_repos_grow_only_as_far_as_each_needs():
    projects = {"a": proj("/one/app"), "b": proj("/two/app"), "c": proj("/x/two/app")}
    snap = snapshot(sess("a"), sess("b"), sess("c"), projects=projects)
    roots = ["a", "b", "c"]
    assert [label for _, label, _ in
            blocks_of(snap, roots, flat(*roots), Group.PROJECT)] == [
        "one/app", "two/app", "x/two/app",
    ]


def test_a_repo_named_unknown_keeps_its_short_label_beside_the_bucket():
    projects = {"a": proj("/x/unknown")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert [label for _, label, _ in
            blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT)] == [
        "unknown", "(unknown)",
    ]


def test_a_project_directory_named_unknown_reads_apart_from_the_bucket():
    # neither key has a segment to grow by, so the labels must differ already
    projects = {"a": Project(encoded="unknown", path="/p/unknown")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("unknown", "unknown", ["a"]),
        ("", "(unknown)", ["b"]),
    ]


def test_labels_that_never_separate_fall_back_to_the_whole_key():
    projects = {"a": proj("/app"), "b": Project(encoded="app", path="/p/app")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert blocks_of(snap, ["a", "b"], flat("a", "b"), Group.PROJECT) == [
        ("/app", "/app", ["a"]),
        ("app", "app", ["b"]),
    ]


# -- by age --------------------------------------------------------------

def test_age_buckets_span_the_four_windows():
    snap = snapshot(sess("fresh", NOW - 3600), sess("week", NOW - 3 * DAY),
                    sess("month", NOW - 20 * DAY), sess("ancient", NOW - 400 * DAY))
    roots = ["fresh", "week", "month", "ancient"]
    assert [label for _, label, _ in
            blocks_of(snap, roots, flat(*roots), Group.AGE)] == list(AGE_LABELS)


def test_the_age_labels_name_rolling_windows():
    assert AGE_LABELS == ("last day", "last week", "last month", "older")


def test_a_fresh_leaf_lifts_its_whole_tree_into_the_last_day():
    snap = snapshot(sess("root", NOW - 400 * DAY), sess("leaf", NOW - 60))
    children = {"root": ["leaf"], "leaf": []}
    assert blocks_of(snap, ["root"], children, Group.AGE) == [
        ("last day", "last day", ["root"]),
    ]


def test_a_bucket_boundary_belongs_to_the_older_window():
    snap = snapshot(sess("a", NOW - DAY), sess("b", NOW - DAY + 1))
    assert [label for _, label, _ in
            blocks_of(snap, ["b", "a"], flat("a", "b"), Group.AGE)] == [
        "last day", "last week",
    ]


def test_a_root_missing_from_the_snapshot_ages_by_the_epoch():
    assert blocks_of(Snapshot(), ["ghost"], {}, Group.AGE, now=100 * DAY) == [
        ("older", "older", ["ghost"]),
    ]


# -- order ---------------------------------------------------------------

def test_age_blocks_run_youngest_first_whatever_the_root_order():
    snap = snapshot(sess("old", NOW - 400 * DAY), sess("mid", NOW - 3 * DAY),
                    sess("new", NOW - 60))
    roots = ["old", "mid", "new"]
    assert [label for _, label, _ in
            blocks_of(snap, roots, flat(*roots), Group.AGE)] == [
        "last day", "last week", "older",
    ]


def test_project_blocks_follow_the_incoming_roots():
    projects = {"a": proj("/one", label="one"), "b": proj("/two", label="two")}
    snap = snapshot(sess("a"), sess("b"), projects=projects)
    assert [key for key, _, _ in
            blocks_of(snap, ["b", "a"], flat("a", "b"), Group.PROJECT)] == ["/two", "/one"]


def test_roots_keep_their_order_inside_a_block():
    projects = {sid: proj("/repo/app", label="app") for sid in ("a", "b", "c")}
    snap = snapshot(sess("a"), sess("b"), sess("c"), projects=projects)
    roots = ["c", "a", "b"]
    assert blocks_of(snap, roots, flat(*roots), Group.PROJECT) == [
        ("/repo/app", "app", ["c", "a", "b"]),
    ]


def test_roots_keep_their_order_inside_an_age_block():
    snap = snapshot(sess("a", NOW - 3600), sess("b", NOW - 7200))
    roots = ["b", "a"]
    assert blocks_of(snap, roots, flat(*roots), Group.AGE) == [
        ("last day", "last day", ["b", "a"]),
    ]


def test_a_returning_key_rejoins_its_block():
    projects = {"a": proj("/one", label="one"), "b": proj("/two", label="two"),
                "c": proj("/one", label="one")}
    snap = snapshot(sess("a"), sess("b"), sess("c"), projects=projects)
    roots = ["a", "b", "c"]
    assert blocks_of(snap, roots, flat(*roots), Group.PROJECT) == [
        ("/one", "one", ["a", "c"]),
        ("/two", "two", ["b"]),
    ]


def test_no_roots_at_all():
    assert partition(Snapshot(), [], {}, Group.PROJECT) == []
    assert partition(Snapshot(), [], {}, Group.NONE)[0].roots == []
