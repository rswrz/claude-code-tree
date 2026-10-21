"""Column table, widths and colouring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cct.layout import (
    LABEL_MIN,
    build_list,
    build_rows,
    dpad,
    dtrunc,
    dw,
    fmt_age,
    fmt_ctx,
    fmt_dur,
)
from cct.group import AGE_LABELS
from cct.model import Agent, Group, Project, Session, Snapshot, TreeRow
from cct.theme import Style, plain

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def styled(row) -> list[tuple[str, str]]:
    return [(text, style.name) for text, style in row]


def style_at(row, needle: str) -> str:
    """The style of the first span containing ``needle``."""
    for text, style in row:
        if needle in text:
            return style.name
    raise AssertionError(f"{needle!r} not in {plain(row)!r}")


def session(sid: str, **kw) -> Session:
    base = {
        "mtime": 1_000_000.0,
        "count": 4,
        "users": 3,
        "first": "some session",
        "branch": "main",
        "ctx": 12_345,
        "ts_min": T0,
        "ts_max": T0 + timedelta(hours=1),
    }
    return Session(sid=sid, **(base | kw))


def snapshot(*sessions: Session, **kw) -> Snapshot:
    return Snapshot(sessions={s.sid: s for s in sessions}, **kw)


def rows_for(snap: Snapshot, tree: list[TreeRow], width: int = 120, project: bool = False):
    return build_rows(snap, tree, width, project)


def flat(sid: str = "aaaaaaaa-1111-2222-3333-444444444444") -> list[TreeRow]:
    return [TreeRow(sid=sid, prefix="", connector="├─ ", depth=0)]


class TestWidth:
    def test_ascii(self):
        assert dw("abc") == 3

    def test_wide_characters_count_double(self):
        assert dw("日本") == 4

    def test_combining_marks_are_free(self):
        assert dw("é") == 1

    def test_box_drawing_is_narrow(self):
        assert dw("│  ├─ ") == 6


class TestTruncate:
    def test_short_enough_is_untouched(self):
        assert dtrunc("abc", 5) == "abc"

    def test_exact_fit_is_untouched(self):
        assert dtrunc("abc", 3) == "abc"

    def test_longer_gets_an_ellipsis(self):
        assert dtrunc("abcdef", 4) == "abc…"
        assert dw(dtrunc("abcdef", 4)) == 4

    def test_wide_characters_never_overflow(self):
        assert dw(dtrunc("日本語です", 5)) <= 5

    def test_non_positive_width_is_empty(self):
        assert dtrunc("abc", 0) == ""
        assert dtrunc("abc", -1) == ""


class TestPad:
    def test_left_align(self):
        assert dpad("ab", 5, "<") == "ab   "

    def test_right_align(self):
        assert dpad("ab", 5, ">") == "   ab"

    def test_already_wide_enough(self):
        assert dpad("abcdef", 3, "<") == "abcdef"

    def test_wide_characters(self):
        assert dpad("日", 4, ">") == "  日"


class TestFormatters:
    @pytest.mark.parametrize("delta,expected", [
        (5, "5s ago"),
        (59, "59s ago"),
        (60, "1m ago"),
        (3599, "59m ago"),
        (3600, "1h ago"),
        (86399, "23h ago"),
        (86400, "1d ago"),
        (86400 * 29, "29d ago"),
        (86400 * 30, "1mo ago"),
        (86400 * 364, "12mo ago"),
        (86400 * 365, "1y ago"),
    ])
    def test_age(self, delta, expected):
        assert fmt_age(1_000_000.0, now=1_000_000.0 + delta) == expected

    def test_duration_needs_two_distinct_stamps(self):
        assert fmt_dur(None, T0) == ""
        assert fmt_dur(T0, None) == ""
        assert fmt_dur(T0, T0) == ""

    @pytest.mark.parametrize("secs,expected", [
        (30, "<1m"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h0m"),
        (28_800, "8h0m"),
        (86_400, "1d0h"),
        (187_200, "2d4h"),
    ])
    def test_duration(self, secs, expected):
        assert fmt_dur(T0, T0 + timedelta(seconds=secs)) == expected

    @pytest.mark.parametrize("n,expected", [
        (None, ""), (0, "0"), (999, "999"), (1000, "1k"), (459_432, "459k"),
    ])
    def test_context(self, n, expected):
        assert fmt_ctx(n) == expected


class TestColumns:
    def test_every_row_is_exactly_the_given_width(self):
        snap = snapshot(session("a"), session("b"))
        tree = [TreeRow("a", "", "├─ ", 0), TreeRow("b", "", "└─ ", 0)]
        for row in rows_for(snap, tree, width=100):
            assert dw(plain(row)) == 100

    def test_column_order(self):
        s = session("a", users=29, ctx=459_000, branch="feat/x", worktree="wt",
                    prs={353}, frames=2)
        snap = snapshot(s, agents={"a": Agent(sid="a", kind="background", state="done")},
                        default_branch="main")
        text = plain(rows_for(snap, [TreeRow("a", "", "├─ ", 0)])[0]).rstrip()
        fields = [f for f in text.split("  ") if f]
        assert fields[0].startswith("├─ some session")
        assert [f.strip() for f in fields[1:]] == [
            "29u", fmt_age(s.mtime), "1h0m", "459k", "⎇\xa0feat/x [wt]", "⧉ 2 #353", "bg:done",
        ]

    def test_empty_columns_disappear(self):
        s = session("a", branch="", ctx=None, users=0, ts_max=T0)
        snap = snapshot(s)
        text = plain(rows_for(snap, flat("a"))[0])
        assert "⎇" not in text and "u " not in text

    def test_project_column_sits_between_label_and_users(self):
        s = session("a")
        snap = snapshot(s, projects={"a": Project(encoded="-x", path="/x", label="webapp")})
        text = plain(rows_for(snap, flat("a"), project=True)[0])
        assert text.index("webapp") < text.index("3u")

    def test_project_column_is_absent_when_not_requested(self):
        s = session("a")
        snap = snapshot(s, projects={"a": Project(encoded="-x", path="/x", label="webapp")})
        assert "webapp" not in plain(rows_for(snap, flat("a"))[0])

    def test_last_column_is_right_trimmed_before_the_final_fill(self):
        s = session("a", branch="", ctx=None)
        long = session("b", branch="", ctx=None, first="x" * 40)
        snap = snapshot(s, long)
        tree = [TreeRow("a", "", "├─ ", 0), TreeRow("b", "", "└─ ", 0)]
        # the age column is last here; both rows still end flush at the width
        for row in rows_for(snap, tree, width=100):
            assert dw(plain(row)) == 100


class TestDegradation:
    def full(self):
        s = session("a", users=29, ctx=459_000, branch="feat/long-branch-name",
                    worktree="a-worktree", prs={353},
                    first="a rather long session title here")
        return snapshot(s, default_branch="main")

    def test_wide_terminal_keeps_everything(self):
        text = plain(rows_for(self.full(), flat("a"), width=160)[0])
        for token in ("29u", "1h0m", "459k", "⎇", "#353"):
            assert token in text

    def test_duration_goes_first(self):
        snap = self.full()
        wide = plain(rows_for(snap, flat("a"), width=160)[0])
        narrow = plain(rows_for(snap, flat("a"), width=90)[0])
        assert "1h0m" in wide and "1h0m" not in narrow
        assert "459k" in narrow

    def test_then_context_then_branch_then_age(self):
        snap = self.full()
        dropped = []
        for width in (90, 84, 78, 42):
            text = plain(rows_for(snap, flat("a"), width=width)[0])
            dropped.append([t for t in ("1h0m", "459k", "⎇", "ago") if t not in text])
        assert dropped[0] == ["1h0m"]
        assert dropped[1] == ["1h0m", "459k"]
        assert dropped[2] == ["1h0m", "459k", "⎇"]
        assert dropped[3] == ["1h0m", "459k", "⎇", "ago"]

    def test_label_stays_above_the_minimum_while_columns_remain(self):
        text = plain(rows_for(self.full(), flat("a"), width=44)[0])
        label = text.split("  ")[0]
        assert dw(label) >= LABEL_MIN

    def test_undroppable_columns_survive_an_impossibly_narrow_terminal(self):
        row = rows_for(self.full(), flat("a"), width=10)[0]
        assert "29u" in plain(row)
        assert dw(plain(row)) >= 38


class TestLabel:
    def test_title_precedence(self):
        s = session("a", custom="custom", ai="ai", first="first")
        assert "custom" in plain(rows_for(snapshot(s), flat("a"))[0])
        s = session("b", ai="ai", first="first")
        assert "ai" in plain(rows_for(snapshot(s), flat("b"))[0])

    def test_long_titles_are_truncated_to_name_max(self):
        s = session("a", first="x" * 200)
        text = plain(rows_for(snapshot(s), flat("a"), width=200)[0])
        assert "…" in text
        assert text.split("  ")[0].count("x") <= 60

    def test_stub_marker(self):
        s = session("a", stub=True)
        row = rows_for(snapshot(s), flat("a"))[0]
        assert " ∅" in plain(row)
        assert style_at(row, "∅") == "MARKER"

    def test_ghost_marker(self):
        s = session("a", ghost=True)
        row = rows_for(snapshot(s), flat("a"))[0]
        assert " ⚠ no transcript" in plain(row)
        assert style_at(row, "⚠") == "MARKER"

    def test_sdk_marker(self):
        s = session("a", first="sdk work", entrypoint="sdk-ts")
        row = rows_for(snapshot(s), flat("a"))[0]
        assert plain(row).split("  ")[0] == "├─ sdk work sdk"
        # the title holds the same text; only the marker after it is coloured
        assert styled(row)[:2] == [("├─ sdk work", "NORMAL"), (" sdk", "MARKER")]

    def test_a_cli_session_has_no_marker(self):
        s = session("a", first="plain", entrypoint="cli")
        assert plain(rows_for(snapshot(s), flat("a"))[0]).split("  ")[0] == "├─ plain"

    def test_a_stub_shows_no_turn_columns(self):
        s = session("a", stub=True)
        text = plain(rows_for(snapshot(s), flat("a"))[0])
        assert "3u" not in text and "459k" not in text and "1h0m" not in text

    def test_a_ghost_shows_no_age(self):
        s = session("a", ghost=True)
        assert "ago" not in plain(rows_for(snapshot(s), flat("a"))[0])

    def test_issue_keys_are_highlighted(self):
        s = session("a", first="ACME-316 ADR")
        row = rows_for(snapshot(s), flat("a"))[0]
        assert style_at(row, "ACME-316") == "ISSUE"

    @pytest.mark.parametrize("title", ["PR 142 merge", "PR-142 merge", "fix #142 now"])
    def test_pr_mentions_are_highlighted(self, title):
        s = session("a", first=title)
        row = rows_for(snapshot(s), flat("a"))[0]
        assert style_at(row, "142") == "PR_KEYWORD"


class TestTreeColouring:
    def tree(self):
        return [
            TreeRow("a", "", "├─ ", 0),
            TreeRow("b", "│  ", "└─ ", 1),
        ]

    def test_root_rows_use_the_default_colour(self):
        snap = snapshot(session("a"), session("b"))
        row = rows_for(snap, self.tree())[0]
        assert all(style is Style.NORMAL for _, style in row)

    def test_child_rows_are_dimmed(self):
        snap = snapshot(session("a"), session("b"))
        row = rows_for(snap, self.tree())[1]
        assert style_at(row, "some session") == "CHILD"

    def test_the_skeleton_left_of_the_connector_keeps_the_default_colour(self):
        snap = snapshot(session("a"), session("b"))
        row = rows_for(snap, self.tree())[1]
        assert row[0] == ("│  ", Style.TREE)
        assert style_at(row, "└─ ") == "CHILD"


class TestBranchColumn:
    def test_default_branch_is_not_highlighted(self):
        s = session("a", branch="main")
        row = rows_for(snapshot(s, default_branch="main"), flat("a"))[0]
        assert style_at(row, "⎇") == "NORMAL"

    def test_other_branches_are_highlighted(self):
        s = session("a", branch="feat/x")
        row = rows_for(snapshot(s, default_branch="main"), flat("a"))[0]
        assert style_at(row, "⎇") == "BRANCH"

    def test_unknown_default_branch_highlights_nothing(self):
        s = session("a", branch="feat/x")
        row = rows_for(snapshot(s), flat("a"))[0]
        assert style_at(row, "⎇") == "NORMAL"

    def test_branch_carries_a_no_break_space(self):
        s = session("a", branch="main")
        assert "⎇\xa0main" in plain(rows_for(snapshot(s), flat("a"))[0])

    def test_worktree_is_bracketed(self):
        s = session("a", branch="main", worktree="feature-42-media")
        assert "⎇\xa0main [feature-42-me…]" in plain(rows_for(snapshot(s), flat("a"))[0])

    def test_worktree_without_a_branch_stands_alone(self):
        s = session("a", branch="", worktree="wt")
        assert "[wt]" in plain(rows_for(snapshot(s), flat("a"))[0])


class TestLinksColumn:
    def test_a_single_artifact_has_no_count(self):
        s = session("a", frames=1)
        row = rows_for(snapshot(s), flat("a"))[0]
        assert "⧉" in plain(row)
        assert style_at(row, "⧉") == "ISSUE"

    def test_several_artifacts_are_counted(self):
        s = session("a", frames=3)
        row = rows_for(snapshot(s), flat("a"))[0]
        assert "⧉ 3" in plain(row)
        assert style_at(row, "⧉ 3") == "ISSUE"

    def test_one_pr_is_named(self):
        s = session("a", prs={353})
        row = rows_for(snapshot(s), flat("a"))[0]
        assert style_at(row, "#353") == "PR"

    def test_several_prs_are_counted(self):
        s = session("a", prs={1, 2, 3})
        row = rows_for(snapshot(s), flat("a"))[0]
        assert style_at(row, "3 PRs") == "PR"

    def test_a_count_after_the_artifact_glyph_is_not_read_as_prs(self):
        s = session("a", frames=2, prs={1, 2})
        row = rows_for(snapshot(s), flat("a"))[0]
        assert "⧉ 2 2 PRs" in plain(row)
        assert style_at(row, "⧉ 2") == "ISSUE"
        assert style_at(row, "2 PRs") == "PR"

    def test_pr_numbers_from_the_job_state_are_merged_in(self):
        s = session("a", prs={7})
        snap = snapshot(s, jobs={"a": {"children": [{"kind": "pr", "id": 9}]}})
        assert "2 PRs" in plain(rows_for(snap, flat("a"))[0])

    def test_a_job_pr_alone_is_enough(self):
        snap = snapshot(session("a"), jobs={"a": {"children": [
            {"kind": "pr", "href": "https://example.test/x/pull/42"}]}})
        assert "#42" in plain(rows_for(snap, flat("a"))[0])


class TestBadge:
    def test_no_agent_no_badge(self):
        text = plain(rows_for(snapshot(session("a")), flat("a"))[0])
        assert "bg" not in text and "live" not in text

    @pytest.mark.parametrize("state,style", [
        ("done", "STATE_DONE"), ("blocked", "STATE_WARN"),
        ("failed", "STATE_BAD"), ("working", "STATE_OK"),
    ])
    def test_background_state(self, state, style):
        snap = snapshot(session("a"), agents={
            "a": Agent(sid="a", kind="background", state=state, live=True)})
        row = rows_for(snap, flat("a"))[0]
        assert f"bg:{state}" in plain(row)
        assert style_at(row, f"bg:{state}") == style

    def test_a_working_background_job_without_a_worker_reads_as_broken(self):
        snap = snapshot(session("a"), agents={
            "a": Agent(sid="a", kind="background", state="working")})
        row = rows_for(snap, flat("a"))[0]
        assert "bg:working" in plain(row)
        assert style_at(row, "bg:working") == "STATE_BAD"

    @pytest.mark.parametrize("live", [True, False])
    @pytest.mark.parametrize("state,style", [
        ("blocked", "STATE_WARN"), ("done", "STATE_DONE"),
    ])
    def test_a_settled_background_job_ignores_liveness(self, state, style, live):
        snap = snapshot(session("a"), agents={
            "a": Agent(sid="a", kind="background", state=state, live=live)})
        assert style_at(rows_for(snap, flat("a"))[0], f"bg:{state}") == style

    def test_stateless_background_job(self):
        # the colon is what makes the badge match the 'bg:' filter that counts it
        snap = snapshot(session("a"), agents={"a": Agent(sid="a", kind="background")})
        assert "bg:" in plain(rows_for(snap, flat("a"))[0])

    def test_a_live_interactive_session(self):
        snap = snapshot(session("a"),
                        agents={"a": Agent(sid="a", status="waiting", live=True)})
        row = rows_for(snap, flat("a"))[0]
        assert "live:waiting" in plain(row)
        assert style_at(row, "live:waiting") == "STATE_WARN"

    def test_statusless_live_session(self):
        snap = snapshot(session("a"), agents={"a": Agent(sid="a", live=True)})
        row = rows_for(snap, flat("a"))[0]
        assert "live:" in plain(row)
        assert style_at(row, "live:") == "STATE_OK"

    def test_an_interactive_session_without_a_pid_gets_no_badge(self):
        snap = snapshot(session("a"), agents={"a": Agent(sid="a", status="waiting")})
        assert "live" not in plain(rows_for(snap, flat("a"))[0])


class TestGrouping:
    NOW = 2_000_000.0

    def snap(self):
        return snapshot(
            session("a", mtime=self.NOW - 3600),
            session("b", mtime=self.NOW - 20 * 86400),
            session("c", mtime=self.NOW - 3600),
            projects={"a": Project(encoded="-webapp", path="/p/webapp",
                                   cwd="/repo/webapp", label="webapp"),
                      "b": Project(encoded="-api", path="/p/api", cwd="/repo/api",
                                   label="api"),
                      "c": Project(encoded="-webapp", path="/p/webapp",
                                   cwd="/repo/webapp", label="webapp")})

    def listing(self, group, roots=None, children=None, width=120, project=True):
        return build_list(self.snap(), roots or ["a", "b"], children or {"a": [], "b": []},
                          width, project, group, now=self.NOW)

    def test_no_grouping_draws_no_header(self):
        listing = self.listing(Group.NONE)
        assert listing.sids == ["a", "b"]
        assert len(listing.rows) == 2

    def test_a_header_carries_no_sid(self):
        listing = self.listing(Group.PROJECT)
        assert len(listing.rows) == len(listing.sids)
        assert listing.sids == ["", "a", "", "b"]

    def test_a_header_names_its_block_and_counts_it(self):
        rows = self.listing(Group.PROJECT).rows
        assert plain(rows[0]).startswith("  webapp (1) ─")
        assert plain(rows[2]).startswith("  api (1) ─")

    def test_a_header_counts_the_whole_block_not_its_roots(self):
        listing = self.listing(Group.PROJECT, children={"a": ["c"], "b": [], "c": []})
        assert plain(listing.rows[0]).startswith("  webapp (2) ")
        assert listing.sids == ["", "a", "c", "", "b"]

    def test_every_row_carries_the_label_of_its_block(self):
        listing = self.listing(Group.PROJECT)
        assert listing.labels == ["webapp", "webapp", "api", "api"]

    def test_a_worktree_row_carries_its_repo_and_not_the_worktree_name(self):
        snap = snapshot(session("a"), session("b"), projects={
            "a": Project(encoded="-wt-one", path="/p/wt-one",
                         cwd="/repo/webapp/.claude/worktrees/wt-one", label="wt-one"),
            "b": Project(encoded="-api", path="/p/api", cwd="/repo/api", label="api")})
        listing = build_list(snap, ["a", "b"], {"a": [], "b": []}, 120, True, Group.PROJECT)
        assert plain(listing.rows[0]).startswith("  webapp (1) ")
        assert listing.labels == ["webapp", "webapp", "api", "api"]

    def test_a_header_is_styled_label_count_rule(self):
        row = self.listing(Group.PROJECT).rows[0]
        assert styled(row) == [("  webapp", "GROUP"), (" (1)", "COUNTER"),
                               (" " + "─" * 107, "BORDER")]

    def test_a_long_label_is_truncated(self):
        name = "l" * 80
        snap = snapshot(session("a"), session("b"), projects={
            "a": Project(encoded="-x", path="/x", cwd=f"/repo/{name}", label=name),
            "b": Project(encoded="-y", path="/y", cwd="/repo/short", label="short")})
        row = build_list(snap, ["a", "b"], {"a": [], "b": []}, 40, False, Group.PROJECT).rows[0]
        assert "…" in plain(row) and dw(plain(row)) == 40

    @pytest.mark.parametrize("group", [Group.PROJECT, Group.AGE])
    def test_a_single_block_draws_no_header(self, group):
        listing = self.listing(group, roots=["a"], children={"a": []})
        assert listing.sids == ["a"]
        assert len(listing.rows) == 1

    def test_a_single_block_still_labels_its_rows(self):
        # nothing is drawn, but the block is still what the rows belong to
        listing = self.listing(Group.PROJECT, roots=["a"], children={"a": []})
        assert listing.labels == ["webapp"]

    def test_a_single_block_keeps_the_project_column(self):
        # with no header drawn, the column is the only thing naming the project
        listing = self.listing(Group.PROJECT, roots=["a"], children={"a": []})
        assert "webapp" in plain(listing.rows[0])

    @pytest.mark.parametrize("group", [Group.NONE, Group.PROJECT, Group.AGE])
    def test_every_row_is_exactly_the_given_width(self, group):
        for row in self.listing(group, width=100).rows:
            assert dw(plain(row)) == 100

    def test_age_blocks_follow_the_subtree(self):
        listing = self.listing(Group.AGE)
        assert plain(listing.rows[0]).startswith(f"  {AGE_LABELS[0]} (1) ")
        assert plain(listing.rows[2]).startswith(f"  {AGE_LABELS[2]} (1) ")

    def test_the_given_clock_governs_the_age_column_too(self):
        row = plain(self.listing(Group.AGE).rows[1])
        assert "1h ago" in row

    def test_grouping_by_project_gives_the_column_back_to_the_label(self):
        rows = self.listing(Group.PROJECT).rows
        assert "webapp" in plain(rows[0])
        assert "webapp" not in plain(rows[1])

    def test_grouping_by_age_keeps_the_project_column(self):
        assert "webapp" in plain(self.listing(Group.AGE).rows[1])


def test_a_session_missing_from_the_snapshot_still_renders():
    row = rows_for(Snapshot(), flat("deadbeef-0000-0000-0000-000000000000"))[0]
    assert "deadbeef" in plain(row)


def test_no_rows_at_all():
    assert build_rows(Snapshot(), [], 100, False) == []


def test_spans_are_coalesced():
    snap = snapshot(session("a"))
    row = rows_for(snap, flat("a"))[0]
    assert all(a[1] is not b[1] for a, b in zip(row, row[1:]))
    assert all(text for text, _ in row)
