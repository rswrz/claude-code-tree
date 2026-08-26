"""Interaction layer without a tty: colour degradation, key handling, chrome."""

from __future__ import annotations

import curses
import types

import pytest

from cct import paths, store
from cct import layout
from cct.layout import BR_MAX, dtrunc, dw
from cct.model import Agent, Group, Project, Session, Snapshot, Sort
from cct.theme import Style
from cct.ui import app, screen, widgets


# --------------------------------------------------------------------------- #
# colour degradation
# --------------------------------------------------------------------------- #


def test_basic_color_collapses_the_256_palette():
    assert screen.basic_color(-1) == -1
    assert screen.basic_color(3) == 3
    assert screen.basic_color(12) == 4                 # bright blue -> blue
    assert screen.basic_color(196) == 1                # cube red
    assert screen.basic_color(46) == 2                 # cube green
    assert screen.basic_color(21) == 4                 # cube blue
    assert screen.basic_color(16) == 0                 # cube black
    assert screen.basic_color(231) == 7                # cube white
    assert screen.basic_color(232) == 0                # dark grey ramp
    assert screen.basic_color(255) == 7                # light grey ramp


def _palette(colors=256, pairs=256):
    calls = []
    palette = screen.Palette(colors, pairs, lambda n, fg, bg: calls.append((n, fg, bg)))
    return palette, calls


def test_pairs_are_allocated_once_and_reused():
    palette, calls = _palette()
    first = palette.pair_for(161, 236)
    assert palette.pair_for(161, 236) == first
    assert palette.pair_for(39, -1) != first
    assert calls == [(1, 161, 236), (2, 39, -1)]


def test_pair_allocation_stops_at_the_terminal_limit():
    palette, _ = _palette(pairs=3)
    assert palette.pair_for(1, -1) == 1
    assert palette.pair_for(2, -1) == 2
    assert palette.pair_for(3, -1) == 0                # out of pairs, fall back
    assert palette.pair_for(1, -1) == 1                # cached ones still work


def test_an_8_colour_terminal_gets_the_nearest_basic_colour():
    palette, calls = _palette(colors=8)
    palette.pair_for(161, 236)
    assert calls == [(1, 1, 0)]


def test_a_colourless_terminal_keeps_attributes_only():
    palette, calls = _palette(colors=0)
    assert palette.pair_for(161, 236) == 0
    assert calls == []
    assert palette.attr(Style.BOLD) & curses.A_BOLD
    assert palette.attr(Style.DIM) & curses.A_DIM
    assert palette.attr(Style.HIT) & curses.A_REVERSE


def test_default_on_default_needs_no_pair():
    palette, calls = _palette()
    assert palette.pair_for(-1, -1) == 0
    assert calls == []


def test_a_failing_init_pair_degrades_instead_of_raising():
    def boom(n, fg, bg):
        raise curses.error("nope")

    palette = screen.Palette(256, 256, boom)
    assert palette.pair_for(1, 2) == 0


def test_cut_respects_wide_characters():
    assert screen.cut("hello", 3) == "hel"
    assert screen.cut("あいう", 4) == "あい"
    assert screen.cut("あいう", 3) == "あ"
    assert screen.cut("hello", 0) == ""
    assert screen.cut("hi", 99) == "hi"


# --------------------------------------------------------------------------- #
# escape sequences
# --------------------------------------------------------------------------- #


def reader(tail: str):
    """Feed a decoder the bytes that follow an ESC, then nothing."""
    chars = iter(tail)
    return lambda: next(chars, None)


@pytest.mark.parametrize("csi, ss3, key", [
    ("[A", "OA", curses.KEY_UP),
    ("[B", "OB", curses.KEY_DOWN),
    ("[C", "OC", curses.KEY_RIGHT),
    ("[D", "OD", curses.KEY_LEFT),
    ("[H", "OH", curses.KEY_HOME),
    ("[F", "OF", curses.KEY_END),
])
def test_both_cursor_encodings_decode_to_the_same_key(csi, ss3, key):
    assert screen.decode_escape(reader(csi)) == key
    assert screen.decode_escape(reader(ss3)) == key


@pytest.mark.parametrize("tail, key", [
    ("[1~", curses.KEY_HOME),
    ("[7~", curses.KEY_HOME),
    ("[4~", curses.KEY_END),
    ("[8~", curses.KEY_END),
    ("[5~", curses.KEY_PPAGE),
    ("[6~", curses.KEY_NPAGE),
    ("[3~", curses.KEY_DC),
    ("[1;2A", curses.KEY_SR),
    ("[1;2B", curses.KEY_SF),
    ("[1;2C", curses.KEY_SRIGHT),
    ("[1;2D", curses.KEY_SLEFT),
    ("[5;2~", curses.KEY_SPREVIOUS),
    ("[6;2~", curses.KEY_SNEXT),
])
def test_csi_sequences_decode(tail, key):
    assert screen.decode_escape(reader(tail)) == key


def test_a_solitary_escape_is_still_escape():
    assert screen.decode_escape(reader("")) == "\x1b"


def test_an_unmapped_sequence_is_swallowed_whole():
    # a bracketed-paste marker must not type "[200~" into the query
    assert screen.decode_escape(reader("[200~")) is None
    assert screen.decode_escape(reader("[?1000h")) is None
    assert screen.decode_escape(reader("[15;5~")) is None


def test_alt_key_and_truncated_sequences_are_ignored():
    assert screen.decode_escape(reader("a")) is None
    assert screen.decode_escape(reader("[")) is None
    assert screen.decode_escape(reader("[1;")) is None
    assert screen.decode_escape(reader("O")) is None


def test_a_key_ncurses_already_assembled_passes_through():
    assert screen.decode_escape(lambda: curses.KEY_DOWN) == curses.KEY_DOWN


class KeyWin:
    """A window that hands out a scripted key stream and records its timeouts."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.timeouts = []

    def timeout(self, ms):
        self.timeouts.append(ms)

    def get_wch(self):
        if not self.keys:
            raise curses.error("no input")
        key = self.keys.pop(0)
        if key is None:
            raise curses.error("no input")
        return key


def _screen_with(keys):
    scr = screen.Screen.__new__(screen.Screen)
    scr.stdscr = KeyWin(keys)
    scr.palette = screen.Palette(0, 0, lambda *a: None)
    return scr


def test_get_key_assembles_a_split_arrow_sequence():
    scr = _screen_with(["\x1b", "[", "B"])
    assert scr.get_key(500) == curses.KEY_DOWN
    assert scr.stdscr.timeouts == [500, screen.ESC_PEEK_MS, 500]


def test_get_key_returns_a_lone_escape():
    scr = _screen_with(["\x1b"])
    assert scr.get_key(500) == "\x1b"
    assert scr.stdscr.timeouts[-1] == 500


def test_get_key_passes_ordinary_keys_straight_through():
    scr = _screen_with(["a", curses.KEY_UP])
    assert scr.get_key(500) == "a"
    assert scr.get_key(500) == curses.KEY_UP
    assert screen.ESC_PEEK_MS not in scr.stdscr.timeouts


def test_get_key_reports_a_timeout_as_none():
    assert _screen_with([]).get_key(500) is None


# --------------------------------------------------------------------------- #
# terminal handover
# --------------------------------------------------------------------------- #


class FakeWin:
    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.log.append(f"win.{name}")

        return call

    def getmaxyx(self):
        return 40, 120


class FakeCurses(types.SimpleNamespace):
    error = curses.error

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.log.append(name)

        return call


@pytest.fixture
def suspendable(monkeypatch):
    log = []
    fake = FakeCurses(log=log)
    monkeypatch.setattr(screen, "curses", fake)
    scr = screen.Screen.__new__(screen.Screen)
    scr.stdscr = FakeWin(log)
    scr.palette = screen.Palette(0, 0, lambda *a: None)
    return scr, log


def test_suspend_hands_over_and_restores(suspendable):
    scr, log = suspendable
    with scr.suspend():
        log.append("--child--")
    assert log.index("endwin") < log.index("--child--")
    assert log.index("--child--") < log.index("reset_prog_mode")
    assert "doupdate" in log
    assert "raw" in log[log.index("--child--"):]


def test_suspend_restores_after_the_child_blows_up(suspendable):
    scr, log = suspendable
    with pytest.raises(KeyboardInterrupt):
        with scr.suspend():
            raise KeyboardInterrupt
    assert "reset_prog_mode" in log
    assert "doupdate" in log


def test_run_reports_a_missing_binary(suspendable):
    scr, log = suspendable
    assert scr.run(["definitely-not-a-real-binary-xyz"]) == 127
    assert "reset_prog_mode" in log


def test_run_returns_the_child_exit_status(suspendable):
    scr, _ = suspendable
    assert scr.run(["/bin/sh", "-c", "exit 7"]) == 7


# --------------------------------------------------------------------------- #
# widgets
# --------------------------------------------------------------------------- #


def test_box_geometry_centres_and_fits():
    top, left, height, width = widgets.box_geometry(40, 120, 30, 4)
    assert (height, width) == (6, 34)
    assert (top, left) == (17, 43)


def test_box_geometry_never_leaves_the_screen():
    top, left, height, width = widgets.box_geometry(4, 20, 200, 40)
    assert (top, left) == (0, 0)
    assert height <= 4 and width <= 20


def test_delete_word_drops_the_trailing_word():
    assert widgets.delete_word("one two three") == "one two "
    assert widgets.delete_word("one ") == ""
    assert widgets.delete_word("one") == ""
    assert widgets.delete_word("") == ""


def test_edit_line_handles_typing_and_erasing():
    assert widgets.edit_line("ab", "c") == ("abc", True)
    assert widgets.edit_line("abc", "\x7f") == ("ab", True)
    assert widgets.edit_line("abc", curses.KEY_BACKSPACE) == ("ab", True)
    assert widgets.edit_line("a b", "\x17") == ("a ", True)
    assert widgets.edit_line("a b", "\x15") == ("", True)
    assert widgets.edit_line("ab", curses.KEY_F1) == ("ab", False)
    assert widgets.edit_line("ab", "\x06") == ("ab", False)


def test_wrap_text_wraps_greedily_at_spaces():
    assert widgets.wrap_text("the quick brown fox", 10) == ["the quick", "brown fox"]


def test_wrap_text_of_nothing_is_one_empty_line():
    assert widgets.wrap_text("", 10) == [""]


def test_wrap_text_keeps_the_line_a_trailing_newline_opens():
    assert widgets.wrap_text("hi\n", 10) == ["hi", ""]


def test_wrap_text_splits_on_explicit_newlines():
    assert widgets.wrap_text("one\ntwo", 10) == ["one", "two"]
    assert widgets.wrap_text("a\n\nb", 10) == ["a", "", "b"]


def test_wrap_text_hard_splits_an_overlong_word():
    assert widgets.wrap_text("abcdefgh", 3) == ["abc", "def", "gh"]


def test_wrap_text_counts_wide_characters_as_two_cells():
    assert dw("あいうえお") == 10
    lines = widgets.wrap_text("あいうえお", 6)
    assert lines == ["あいう", "えお"]
    assert all(dw(line) <= 6 for line in lines)


def test_wrap_text_survives_a_zero_width():
    assert widgets.wrap_text("ab", 0) == ["a", "b"]


class AreaScreen:
    """A Screen that draws nowhere and hands out a scripted key stream."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.stdscr = FakeWin([])
        self.size = (40, 120)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            return None

        return call

    def get_key(self, timeout_ms=-1):
        return self.keys.pop(0)


def area(keys, **kw):
    return widgets.text_area(AreaScreen(keys), "Title", "> ", **kw)


def test_text_area_returns_what_was_typed():
    assert area(["h", "i", "\r"]) == "hi"


def test_text_area_cancels_on_escape_and_on_ctrl_c():
    assert area(["h", "\x1b"]) is None
    assert area(["h", "\x03"]) is None


def test_text_area_inserts_a_newline_when_multiline():
    assert area(["a", "\n", "b", "\r"], multiline=True) == "a\nb"


def test_text_area_ignores_ctrl_j_on_a_single_line():
    assert area(["a", "\n", "b", "\r"]) == "ab"


def test_text_area_starts_from_the_initial_text():
    assert area(["\r"], initial="keep me") == "keep me"


def test_text_area_backspace_removes_a_newline():
    assert area(["a", "\n", "\x7f", "b", "\r"], multiline=True) == "ab"


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


def state(**kw):
    base = dict(count=10, total=10, page=4)
    base.update(kw)
    return app.UIState(**base)


def test_selection_clamps_and_scrolls():
    st = state()
    st.select(0)
    assert (st.sel, st.top) == (0, 0)
    st.move(5)
    assert (st.sel, st.top) == (5, 2)
    st.move(99)
    assert (st.sel, st.top) == (9, 6)
    st.move(-99)
    assert (st.sel, st.top) == (0, 0)


def test_selection_on_an_empty_list_stays_put():
    st = state(count=0)
    st.move(3)
    assert (st.sel, st.top) == (0, 0)


def test_selection_steps_over_a_header_in_the_direction_of_travel():
    st = state(skip=frozenset({2}))
    st.select(1)
    st.move(1)
    assert st.sel == 3
    st.move(-1)
    assert st.sel == 1


def test_selection_steps_over_two_adjacent_headers():
    st = state(skip=frozenset({2, 3}))
    st.select(1)
    st.move(1)
    assert st.sel == 4
    st.move(-1)
    assert st.sel == 1


def test_a_header_at_the_top_hands_the_selection_back_downwards():
    st = state(skip=frozenset({0}))
    st.select(0)
    assert st.sel == 1
    st.move(-99)
    assert (st.sel, st.top) == (1, 0)


def test_a_header_at_the_bottom_hands_the_selection_back_upwards():
    st = state(skip=frozenset({9}))
    st.move(99)
    assert st.sel == 8


def test_a_view_of_nothing_but_headers_settles_anyway():
    st = state(count=2, skip=frozenset({0, 1}))
    st.select(0)
    assert st.sel == 0


def test_the_first_block_header_comes_back_with_the_selection():
    st = state(skip=frozenset({0, 5}))
    app.handle_key(st, curses.KEY_END)
    app.handle_key(st, curses.KEY_HOME)
    assert (st.sel, st.top) == (1, 0)


def test_scrolling_up_into_a_block_pulls_its_header_in():
    st = state(skip=frozenset({0, 5}))
    st.select(9, -1)
    assert (st.sel, st.top) == (9, 6)
    st.move(-3)
    assert (st.sel, st.top) == (6, 5)


def test_a_run_of_headers_above_the_selection_comes_along():
    st = state(skip=frozenset({4, 5}))
    st.select(9, -1)
    st.select(6)
    assert (st.sel, st.top) == (6, 4)


def test_a_page_of_one_keeps_the_selection_over_its_header():
    st = state(page=1, skip=frozenset({4}))
    st.select(5)
    assert (st.sel, st.top) == (5, 5)


def test_preview_scroll_clamps_to_the_last_page():
    st = state(preview=True, preview_len=20, preview_page=5)
    st.scroll_preview(99)
    assert st.preview_off == 15
    st.scroll_preview(-99)
    assert st.preview_off == 0


# --------------------------------------------------------------------------- #
# the key table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key, kind", [
    ("\r", app.RESUME),
    (curses.KEY_ENTER, app.RESUME),
    ("\x06", app.FORK),
    ("\x0e", app.NEW),
    ("\x04", app.DISPATCH),
    ("\x12", app.RENAME),
    ("\x18", app.DELETE),
    ("\x14", app.PRUNE),
    ("\x0f", app.LINKS),
    ("\x01", app.TOGGLE_SCOPE),
    ("\x13", app.CYCLE_SORT),
    ("\x05", app.CYCLE_GROUP),
    ("\x0c", app.RELOAD),
    ("\x03", app.QUIT),
    (curses.KEY_RESIZE, app.RESIZE),
])
def test_key_table(key, kind):
    assert app.handle_key(state(), key).kind == kind


def test_ctrl_g_cycles_the_agent_filter():
    assert app.handle_key(state(), "\x07") == app.Effect(app.CYCLE_AGENT)


def test_ctrl_b_needs_a_git_branch():
    assert app.handle_key(state(), "\x02").kind == app.NONE
    effect = app.handle_key(state(branch="feature/x"), "\x02")
    assert effect == app.Effect(app.TOGGLE_TOKEN, "⎇ feature/x")


def test_space_toggles_the_preview_when_the_query_is_idle():
    st = state()
    assert app.handle_key(st, " ").kind == app.PREVIEW
    assert st.preview is True
    assert app.handle_key(st, " ").kind == app.PREVIEW
    assert st.preview is False


def test_space_extends_a_query_that_is_being_typed():
    st = state(query="bg:")
    assert app.handle_key(st, " ").kind == app.QUERY
    assert st.query == "bg: "
    assert st.preview is False
    # a query already ending in a space falls back to the toggle
    assert app.handle_key(st, " ").kind == app.PREVIEW
    assert st.preview is True


def test_esc_closes_the_preview_before_it_quits():
    st = state(preview=True)
    assert app.handle_key(st, "\x1b").kind == app.PREVIEW
    assert st.preview is False
    assert app.handle_key(st, "\x1b").kind == app.QUIT


def test_ctrl_p_toggles_the_preview_whatever_the_query():
    st = state(query="abc")
    assert app.handle_key(st, "\x10").kind == app.PREVIEW
    assert st.preview is True
    assert st.query == "abc"


def test_movement_keys():
    st = state()
    app.handle_key(st, curses.KEY_DOWN)
    app.handle_key(st, "\n")
    assert st.sel == 2
    app.handle_key(st, curses.KEY_UP)
    app.handle_key(st, "\x0b")
    assert st.sel == 0
    app.handle_key(st, curses.KEY_END)
    assert st.sel == 9
    app.handle_key(st, curses.KEY_HOME)
    assert st.sel == 0


def test_page_keys_move_the_list_by_a_page():
    st = state()
    app.handle_key(st, curses.KEY_NPAGE)
    assert st.sel == 4
    app.handle_key(st, curses.KEY_PPAGE)
    assert st.sel == 0


def test_page_keys_still_move_by_screen_rows_over_a_header():
    st = state(skip=frozenset({4}))
    app.handle_key(st, curses.KEY_NPAGE)
    assert (st.sel, st.top) == (5, 2)
    app.handle_key(st, curses.KEY_PPAGE)
    assert (st.sel, st.top) == (1, 1)


def test_home_and_end_land_on_a_session():
    st = state(skip=frozenset({0, 9}))
    app.handle_key(st, curses.KEY_END)
    assert st.sel == 8
    app.handle_key(st, curses.KEY_HOME)
    assert st.sel == 1


def test_page_keys_follow_a_scrolled_preview():
    st = state(preview=True, preview_len=100, preview_page=10, preview_off=5)
    assert app.handle_key(st, curses.KEY_NPAGE).kind == app.PREVIEW
    assert st.preview_off == 15
    assert st.sel == 0


def test_shift_arrows_scroll_the_preview():
    st = state(preview=True, preview_len=50, preview_page=10)
    app.handle_key(st, curses.KEY_SF)
    assert st.preview_off == 1
    app.handle_key(st, curses.KEY_SR)
    assert st.preview_off == 0


def test_typing_builds_the_query():
    st = state()
    for ch in "he/o":
        assert app.handle_key(st, ch).kind == app.QUERY
    assert st.query == "he/o"
    app.handle_key(st, "\x7f")
    assert st.query == "he/"
    app.handle_key(st, "\x15")
    assert st.query == ""
    assert app.handle_key(st, "\x7f").kind == app.NONE


def test_unknown_keys_are_ignored():
    assert app.handle_key(state(), curses.KEY_F5).kind == app.NONE
    assert app.handle_key(state(), None).kind == app.NONE


# --------------------------------------------------------------------------- #
# chrome
# --------------------------------------------------------------------------- #


def plain(row):
    return "".join(text for text, _ in row)


def test_prompt_shows_the_query_after_the_prompt():
    row = app.prompt_row("bg: ACME")
    assert row[0] == ("session > ", Style.PROMPT)
    assert plain(row) == "session > bg: ACME"


def test_status_counts_the_rows_and_names_the_scope():
    row = app.status_row(103, 122, app.Tally(), "/srv/project", False)
    assert plain(row) == "  /srv/project   103/122 sessions"


def test_status_counts_the_projects_in_the_wide_scope():
    # three sessions, two of them in the same project directory
    snap = Snapshot(projects={
        "a": Project(encoded="-srv-one", path="/srv/one"),
        "b": Project(encoded="-srv-one", path="/srv/one"),
        "c": Project(encoded="-srv-two", path="/srv/two"),
    })
    assert plain(app.status_row(9, 9, app.Tally(interactive=9), "/srv/one", True, snap)) == \
        "  all projects (2)   9/9 sessions · 9 interactive"


def test_status_shortens_a_home_relative_target():
    row = app.status_row(1, 1, app.Tally(), paths.HOME + "/code/tool", False)
    assert plain(row) == "  ~/code/tool   1/1 sessions"


def test_status_groups_the_interactive_sessions_before_the_background_agents():
    tally = app.Tally(interactive=6, live=2, background=4,
                      running=1, blocked=1, bad=1, done=1)
    row = app.status_row(10, 10, tally, "/srv/project", False)
    assert plain(row) == ("  /srv/project   10/10 sessions · 6 interactive [2]"
                         " · 4 background [1/1/1/1]")


def _group(row, label):
    """``(count, [slot, ...])`` of the group ``label`` names in a status line."""
    group = next(part for part in plain(row).split(" · ") if label in part)
    count, _, slots = group.partition(" [")
    return int(count.split()[0]), [int(n) for n in slots.rstrip("]").split("/") if slots]


def test_status_slots_add_up_to_the_group_counts():
    tally = app.Tally(interactive=2, live=1, background=6,
                      running=2, blocked=1, bad=2, done=1)
    row = app.status_row(8, 8, tally, "/srv/project", False)
    assert plain(row) == ("  /srv/project   8/8 sessions · 2 interactive [1]"
                         " · 6 background [2/1/2/1]")
    interactive, live = _group(row, "interactive")
    background, slots = _group(row, "background")
    assert interactive + background == 8               # the groups split what is shown
    assert sum(slots) == background                    # and the slots split theirs
    assert sum(live) <= interactive                    # live is a subset, not a split


def test_status_omits_the_interactive_count_when_every_row_is_an_agent():
    tally = app.Tally(background=2, running=1, blocked=1)
    assert plain(app.status_row(2, 2, tally, "/srv/project", False)) == \
        "  /srv/project   2/2 sessions · 2 background [1/1]"


def test_status_drops_the_bracket_when_no_session_is_open():
    assert plain(app.status_row(4, 4, app.Tally(interactive=4), "/srv/project", False)) == \
        "  /srv/project   4/4 sessions · 4 interactive"


def test_status_omits_the_background_group_without_background_agents():
    tally = app.Tally(interactive=1, live=1)
    assert plain(app.status_row(1, 1, tally, "/srv/project", False)) == \
        "  /srv/project   1/1 sessions · 1 interactive [1]"


def test_status_shows_scan_progress():
    row = app.status_row(3, 3, app.Tally(interactive=3), "/srv/project", False,
                         Snapshot(scanning=True, scanned=120, total=500))
    assert plain(row) == "  /srv/project   3/3 sessions · 3 interactive   scanning 120/500"


def test_status_drops_the_counter_noun_before_the_scope_gives_way():
    # narrower than draw will render, so this is a guard: the digits stay whole
    row = app.status_row(7, 9, app.Tally(interactive=7), "/srv/project", False, width=20)
    assert plain(row) == "  /srv/project   7/9"


def test_status_keeps_the_error_when_the_line_is_narrow():
    snap = Snapshot(error="cache unreadable")
    tally = app.Tally(interactive=1, background=1, blocked=1)
    row = app.status_row(7, 9, tally, "/srv/deeply/nested/project", False, snap, width=30)
    assert dw(plain(row)) <= 30
    assert plain(row).endswith("7/9   cache unreadable")
    assert "interactive" not in plain(row) and "background" not in plain(row)
    assert "sessions" not in plain(row)


def test_footer_is_always_two_lines():
    lines = app.footer_lines(200, branch="main", sort=Sort.RECENT)
    assert len(lines) == 2
    assert plain(lines[0]).startswith("Enter resume/attach")
    assert "Ctrl+N new session" in plain(lines[0])
    assert "Ctrl+D background agent" in plain(lines[0])
    assert "Ctrl+B branch=main" in plain(lines[1])
    assert "Ctrl+S sort=recent" in plain(lines[1])


def test_footer_drops_hints_when_the_terminal_is_narrow():
    wide = app.footer_lines(200)
    narrow = app.footer_lines(40)
    assert len(narrow) == 2
    for row in narrow:
        assert dw(plain(row)) <= 40
    assert len(plain(narrow[0])) < len(plain(wide[0]))
    assert plain(narrow[0]).startswith("Enter resume/attach")


def test_footer_caps_the_length_of_the_branch_hint():
    branch = "worktree-tui-status-and-grouping"
    line = plain(app.footer_lines(120, branch=branch)[1])
    assert f"Ctrl+B branch={dtrunc(branch, BR_MAX)}" in line
    assert dw(line) <= 120


@pytest.mark.parametrize("width", [100, 120])
def test_footer_keeps_the_mode_keys_next_to_a_long_branch(width):
    line = plain(app.footer_lines(width, branch="worktree-tui-status-and-grouping")[1])
    assert "Ctrl+S sort=recent" in line
    assert "Ctrl+E group=none" in line
    assert dw(line) <= width


def test_footer_hides_the_branch_hint_outside_a_repo():
    assert "Ctrl+B" not in plain(app.footer_lines(200)[1])


def test_footer_names_the_scope_it_would_switch_to():
    assert "Ctrl+A all projects" in plain(app.footer_lines(200, all_projects=False)[1])
    assert "Ctrl+A this project" in plain(app.footer_lines(200, all_projects=True)[1])


def test_footer_names_the_grouping_in_force():
    assert "Ctrl+E group=none" in plain(app.footer_lines(200)[1])
    assert "Ctrl+E group=project" in plain(app.footer_lines(200, group=Group.PROJECT)[1])


@pytest.mark.parametrize("agent, label", [
    ("", "bg:/live:"),
    ("bg:", "bg:"),
    ("bg:blocked", "bg:blocked"),
    ("bg:done", "bg:done"),
    ("live:", "live:"),
])
def test_footer_names_the_active_agent_filter(agent, label):
    assert f"Ctrl+G {label}" in plain(app.footer_lines(200, agent=agent)[1])


def test_the_agent_filter_is_read_off_the_query():
    assert app.agent_token("adr bg:failed ACME-1") == "bg:failed"
    assert app.agent_token("adr bg:") == "bg:"
    assert app.agent_token("adr live:") == "live:"
    assert app.agent_token("adr") == ""
    assert app.agent_token("bg:running") == ""


def test_ctrl_g_walks_every_agent_filter_and_then_out_again():
    query = "adr"
    walk = []
    for _ in range(len(app.AGENT_CYCLE) + 1):
        query = app.fuzzy.cycle_token(query, app.AGENT_CYCLE)
        walk.append(query)
    assert walk == ["adr bg:", "adr bg:blocked", "adr bg:failed", "adr bg:done",
                    "adr live:", "adr"]


def test_preview_header_labels_the_pane_and_counts_lines():
    label, indicator = app.preview_header("abcdef1234567890", 0, 74)
    assert label == "  abcdef12  "
    assert indicator == "1/74"
    assert app.preview_header("abcdef1234567890", 12, 74)[1] == "13/74"


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #


class ListScreen:
    """A Screen that draws nowhere; the app only asks it how wide it is."""

    def __init__(self, width=120, height=40):
        self.stdscr = FakeWin([])
        self.size = (height, width)
        self.width = width

    def __getattr__(self, name):
        def call(*args, **kwargs):
            return None

        return call


class RecordingScreen(ListScreen):
    """A Screen that keeps the row number of everything drawn on it."""

    def __init__(self, width=120, height=40):
        super().__init__(width, height)
        self.drawn: list[tuple[int, str]] = []

    def draw_row(self, win, y, x, row, width, bg=-1, bold=False):
        self.drawn.append((y, plain(row)))

    def draw_text(self, win, y, x, text, style, width, bg=-1, bold=False):
        self.drawn.append((y, text))


class LaunchingScreen(ListScreen):
    """A Screen that records the command handed to the terminal instead of running it."""

    def __init__(self, width=120, height=40):
        super().__init__(width, height)
        self.ran: list[list[str]] = []

    def run(self, argv, cwd=None):
        self.ran.append(argv)
        return 0


class DummyLoader:
    """Stands in for the scanning thread: it never scans, and says so."""

    def __init__(self):
        self.snap = Snapshot()
        self.reloads = 0

    def snapshot(self):
        return self.snap

    def dirty(self):
        return False

    def request_reload(self):
        self.reloads += 1

    def close(self):
        pass


def two_projects():
    """Two sessions in each of two projects, no forks."""
    titles = {"a": "alpha", "b": "bravo", "c": "charlie", "d": "delta"}
    names = {"a": "one", "b": "one", "c": "two", "d": "two"}
    return Snapshot(
        sessions={sid: Session(sid=sid, first=title, mtime=1_000_000.0)
                  for sid, title in titles.items()},
        projects={sid: Project(encoded=f"-{name}", path=f"/p/{name}",
                               cwd=f"/repo/{name}", label=name)
                  for sid, name in names.items()},
        roots=list(titles),
        children={sid: [] for sid in titles},
    )


def a_repo_and_its_worktree():
    """Two sessions in one repo, the second in a worktree, plus a second repo."""
    cwds = {"a": "/r/cct", "b": "/r/cct/.claude/worktrees/wt-one", "c": "/r/other"}
    return Snapshot(
        sessions={sid: Session(sid=sid, first=sid, mtime=1_000_000.0) for sid in cwds},
        projects={sid: Project(encoded=cwd.replace("/", "-"), path="/p" + cwd, cwd=cwd,
                               label=cwd.rsplit("/", 1)[-1])
                  for sid, cwd in cwds.items()},
        roots=list(cwds),
        children={sid: [] for sid in cwds},
    )


def a_mixed_list():
    """Two interactive sessions, one of them live, and one agent per state."""
    titles = {"a": "alpha north", "b": "bravo south", "c": "gamma north",
              "d": "delta south", "e": "epsilon south", "f": "zeta south"}
    states = {"c": "working", "d": "blocked", "e": "stopped", "f": "done"}
    return Snapshot(
        sessions={sid: Session(sid=sid, first=title, mtime=1_000_000.0)
                  for sid, title in titles.items()},
        agents={"a": Agent(sid="a", live=True, status="working"),
                **{sid: Agent(sid=sid, kind="background", state=state,
                              live=state == "working")
                   for sid, state in states.items()}},
        roots=list(titles),
        children={sid: [] for sid in titles},
    )


def one_background_agent(state, live):
    """A single row, held by a background agent in the state given."""
    return Snapshot(
        sessions={"a": Session(sid="a", first="alpha", mtime=1_000_000.0)},
        agents={"a": Agent(sid="a", kind="background", state=state, live=live)},
        roots=["a"],
        children={"a": []},
    )


def listing_app(monkeypatch, snap, group=Group.NONE, scr=None):
    monkeypatch.setattr(app.gitinfo, "current_branch", lambda cwd: "")
    monkeypatch.setattr(app.App, "_new_loader", lambda self: DummyLoader())
    instance = app.App(scr or ListScreen(), store.Scope.project("/repo/one"), group=group)
    instance.snap = snap
    instance.rebuild()
    return instance


def in_view(instance):
    return [instance.sids[i] for i in instance.view]


def test_an_ungrouped_list_is_all_sessions(monkeypatch):
    instance = listing_app(monkeypatch, two_projects())
    assert in_view(instance) == ["a", "b", "c", "d"]
    assert instance.state.skip == frozenset()
    assert (instance.state.count, instance.state.shown, instance.state.total) == (4, 4, 4)


def test_grouping_splices_headers_into_the_view(monkeypatch):
    instance = listing_app(monkeypatch, two_projects(), Group.PROJECT)
    assert in_view(instance) == ["", "a", "b", "", "c", "d"]
    assert instance.state.skip == frozenset({0, 3})
    assert (instance.state.count, instance.state.shown, instance.state.total) == (6, 4, 4)


def test_a_query_that_empties_a_block_drops_its_header(monkeypatch):
    instance = listing_app(monkeypatch, two_projects(), Group.PROJECT)
    assert plain(instance.rows[instance.view[0]]).startswith("  one (2) ")
    instance.state.query = "charlie"
    instance.refilter()
    assert in_view(instance) == ["", "c"]
    assert instance.state.skip == frozenset({0})
    assert (instance.state.shown, instance.state.total) == (1, 4)
    # the surviving header counts what the query left of its block
    assert plain(instance.rows[instance.view[0]]).startswith("  two (1) ")


def test_a_header_count_follows_the_query_and_comes_back(monkeypatch):
    instance = listing_app(monkeypatch, two_projects(), Group.PROJECT)
    instance.state.query = "alpha"
    instance.refilter()
    assert in_view(instance) == ["", "a"]
    assert plain(instance.rows[instance.view[0]]).startswith("  one (1) ")
    instance.state.query = ""
    instance.refilter()
    assert plain(instance.rows[instance.view[0]]).startswith("  one (2) ")


@pytest.mark.parametrize("group", [Group.NONE, Group.PROJECT, Group.AGE])
@pytest.mark.parametrize("all_projects", [False, True])
def test_the_project_name_is_searchable_whatever_is_drawn(monkeypatch, group, all_projects):
    instance = listing_app(monkeypatch, two_projects(), group)
    instance.state.all_projects = all_projects
    instance.rebuild()
    instance.state.query = "one"
    instance.refilter()
    assert [sid for sid in in_view(instance) if sid] == ["a", "b"]


def test_a_group_label_finds_every_session_it_heads(monkeypatch):
    instance = listing_app(monkeypatch, a_repo_and_its_worktree(), Group.PROJECT)
    assert plain(instance.rows[instance.view[0]]).startswith("  cct (2) ")
    instance.state.query = "cct"
    instance.refilter()
    assert [sid for sid in in_view(instance) if sid] == ["a", "b"]
    assert plain(instance.rows[instance.view[0]]).startswith("  cct (2) ")


def test_a_worktree_name_stays_searchable_under_its_repo_header(monkeypatch):
    instance = listing_app(monkeypatch, a_repo_and_its_worktree(), Group.PROJECT)
    instance.state.query = "wt-one"
    instance.refilter()
    assert [sid for sid in in_view(instance) if sid] == ["b"]


def test_the_chrome_leaves_the_list_every_row_but_five(monkeypatch):
    scr = RecordingScreen(width=120, height=40)
    instance = listing_app(monkeypatch, two_projects(), scr=scr)
    instance.draw()
    drawn: dict[int, list[str]] = {}
    for y, text in scr.drawn:
        drawn.setdefault(y, []).append(text)

    assert sorted(drawn) == [0, 1, 2, 3, 4, 5, 37, 38, 39]
    assert drawn[0][0].startswith("session > ")
    assert drawn[1][0].startswith("  /repo/one   4/4")
    # the gutter goes down before the selected row itself
    assert "alpha" in drawn[2][-1]
    assert "bravo" in drawn[3][0]
    assert drawn[37] == ["─" * 120]
    assert "Enter resume/attach" in drawn[38][0]
    assert "Ctrl+G bg:/live:" in drawn[39][0]
    assert instance.state.page == 40 - 5


def test_a_header_is_never_the_current_session(monkeypatch):
    instance = listing_app(monkeypatch, two_projects(), Group.PROJECT)
    instance.state.sel = 0
    assert instance.current() is None
    instance.state.select(0)
    assert instance.current().sid == "a"


def test_cycling_the_grouping_keeps_the_snapshot_and_the_selection(monkeypatch):
    instance = listing_app(monkeypatch, two_projects())
    snap = instance.snap
    instance.state.select(2)
    assert instance.current().sid == "c"

    instance.cycle_group()
    assert instance.state.group is Group.PROJECT
    assert instance.snap is snap
    assert instance.loader.reloads == 0
    assert instance.current().sid == "c"

    instance.cycle_group()
    assert instance.state.group is Group.AGE
    instance.cycle_group()
    assert instance.state.group is Group.NONE
    assert in_view(instance) == ["a", "b", "c", "d"]


def test_the_tally_describes_the_rows_on_screen(monkeypatch):
    instance = listing_app(monkeypatch, a_mixed_list())
    assert instance.state.tally == app.Tally(interactive=2, live=1, background=4,
                                             running=1, blocked=1, bad=1, done=1)


@pytest.mark.parametrize("query, shown, interactive, background", [
    ("", 6, 2, 4),
    ("bg:", 4, 0, 4),
    ("bg:blocked", 1, 0, 1),
    ("north", 2, 1, 1),
    ("zzznomatch", 0, 0, 0),
])
def test_the_tally_follows_the_query(monkeypatch, query, shown, interactive, background):
    instance = listing_app(monkeypatch, a_mixed_list())
    instance.state.query = query
    instance.refilter()
    tally = instance.state.tally
    assert (tally.interactive, tally.background) == (interactive, background)
    # the groups split what is shown, whatever the query left; the total is global
    assert tally.interactive + tally.background == instance.state.shown == shown
    assert tally.running + tally.blocked + tally.bad + tally.done == tally.background
    assert instance.state.total == 6


def test_the_live_slot_drops_the_live_session_the_query_hides(monkeypatch):
    instance = listing_app(monkeypatch, a_mixed_list())
    assert (instance.state.tally.interactive, instance.state.tally.live) == (2, 1)
    instance.state.query = "bravo"
    instance.refilter()
    assert (instance.state.tally.interactive, instance.state.tally.live) == (1, 0)


@pytest.mark.parametrize("state, live, slot", [
    ("working", True, "running"),
    ("working", False, "bad"),            # claims work, and no worker is doing it
    ("running", False, "bad"),            # state.json says running, the pid is gone
    ("hibernating", True, "running"),     # a state this code has never seen
    ("hibernating", False, "bad"),
    ("blocked", True, "blocked"),         # stuck, and still holding a worker
    ("blocked", False, "blocked"),        # settled: liveness says nothing about it
    ("failed", False, "bad"),
    ("stopped", True, "bad"),
    ("done", True, "done"),
])
def test_a_background_agent_lands_in_one_slot_only(monkeypatch, state, live, slot):
    instance = listing_app(monkeypatch, one_background_agent(state, live))
    tally = instance.state.tally
    assert (tally.interactive, tally.background) == (0, 1)
    assert getattr(tally, slot) == 1
    assert tally.running + tally.blocked + tally.bad + tally.done == 1


@pytest.mark.parametrize("state", ["working", "running", "hibernating"])
def test_a_badge_the_list_paints_red_is_counted_in_the_red_slot(monkeypatch, state):
    # the colour carries liveness, so the status line has to read it the same way
    snap = one_background_agent(state, live=False)
    instance = listing_app(monkeypatch, snap)
    assert layout.badge(snap.agents["a"])[1] is Style.STATE_BAD
    row = app.status_row(1, 1, instance.state.tally, "/repo/one", False)
    assert plain(row).endswith("1 background [1]")
    assert ("1", Style.STATE_BAD) in row


def test_the_agent_filter_reaches_the_badges_that_carry_no_state(monkeypatch):
    # Ctrl+G offers these tokens for rows the tally counts, so every badge the
    # tally counts has to match one of them
    snap = Snapshot(
        sessions={sid: Session(sid=sid, first=sid, mtime=1_000_000.0) for sid in ("a", "b")},
        agents={"a": Agent(sid="a", kind="background"), "b": Agent(sid="b", live=True)},
        roots=["a", "b"],
        children={"a": [], "b": []},
    )
    instance = listing_app(monkeypatch, snap)
    assert instance.state.tally == app.Tally(interactive=1, live=1, background=1, running=1)
    for token, sid in (("bg:", "a"), ("live:", "b")):
        instance.state.query = token
        instance.refilter()
        assert in_view(instance) == [sid]


def test_a_group_header_is_no_session_of_the_tally(monkeypatch):
    instance = listing_app(monkeypatch, two_projects(), Group.PROJECT)
    assert (instance.state.count, instance.state.shown) == (6, 4)
    assert instance.state.tally == app.Tally(interactive=4)


def test_an_agent_the_scope_holds_no_row_for_counts_nowhere(monkeypatch):
    # the daemon knows jobs from elsewhere; the tally is of rows, so they cannot
    # push a count anywhere, least of all below zero
    snap = two_projects()
    snap.agents = {f"x{i}": Agent(sid=f"x{i}", kind="background", state="running")
                   for i in range(9)}
    instance = listing_app(monkeypatch, snap)
    assert instance.state.tally == app.Tally(interactive=4)


def test_the_tally_survives_a_relayout(monkeypatch):
    instance = listing_app(monkeypatch, a_mixed_list())
    instance.state.query = "bg:"
    instance.refilter()
    before = instance.state.tally
    instance.cycle_group()             # a regrouping keeps the query it filtered under
    assert instance.state.tally == before
    instance.rebuild()                 # and so does a resize
    assert instance.state.tally == before


def test_the_status_line_a_query_leaves_still_adds_up(monkeypatch):
    scr = RecordingScreen(width=120, height=40)
    instance = listing_app(monkeypatch, a_mixed_list(), scr=scr)
    instance.state.query = "north"
    instance.refilter()
    instance.draw()
    status = next(text for y, text in scr.drawn if y == 1)
    assert status == "  /repo/one   2/6 sessions · 1 interactive [1] · 1 background [1]"


def test_the_status_line_of_a_query_that_matches_nothing(monkeypatch):
    scr = RecordingScreen(width=120, height=40)
    instance = listing_app(monkeypatch, a_mixed_list(), scr=scr)
    instance.state.query = "zzznomatch"
    instance.refilter()
    instance.draw()
    assert next(text for y, text in scr.drawn if y == 1) == "  /repo/one   0/6 sessions"


def stub_confirm(monkeypatch, answer):
    """Answer every dialog the same way, and keep the lines it put up."""
    asked: list[list[str]] = []

    def confirm(scr, title, lines):
        asked.append(lines)
        return answer

    monkeypatch.setattr(app.widgets, "confirm", confirm)
    return asked


def test_resuming_a_session_another_terminal_holds_asks_first(monkeypatch):
    scr = LaunchingScreen()
    instance = listing_app(monkeypatch, a_mixed_list(), scr=scr)
    asked = stub_confirm(monkeypatch, False)
    instance.state.select(0)               # 'a', interactive and live
    instance.open_session(fork=False)
    assert scr.ran == []
    assert "Status:  working" in asked[0]
    assert any("another terminal" in line for line in asked[0])


def test_resuming_a_session_another_terminal_holds_needs_a_yes(monkeypatch):
    scr = LaunchingScreen()
    instance = listing_app(monkeypatch, a_mixed_list(), scr=scr)
    stub_confirm(monkeypatch, True)
    instance.state.select(0)
    instance.open_session(fork=False)
    assert scr.ran == [["claude", "--resume", "a"]]


@pytest.mark.parametrize("sel, argv", [
    (1, ["claude", "--resume", "b"]),      # interactive, no terminal on it
    (2, ["claude", "attach", "c"]),        # a background agent attaches as ever
])
def test_opening_a_session_no_terminal_holds_asks_nothing(monkeypatch, sel, argv):
    scr = LaunchingScreen()
    instance = listing_app(monkeypatch, a_mixed_list(), scr=scr)
    asked = stub_confirm(monkeypatch, False)
    instance.state.select(sel)
    instance.open_session(fork=False)
    assert asked == []
    assert scr.ran == [argv]


def test_forking_a_live_session_asks_nothing(monkeypatch):
    # a fork is a session of its own, so it never lands in the other terminal
    scr = LaunchingScreen()
    instance = listing_app(monkeypatch, a_mixed_list(), scr=scr)
    asked = stub_confirm(monkeypatch, False)
    instance.state.select(0)
    instance.open_session(fork=True)
    assert asked == []
    assert scr.ran == [["claude", "--resume", "a", "--fork-session"]]
