"""Interaction layer without a tty: colour degradation, key handling, chrome."""

from __future__ import annotations

import curses
import types

import pytest

from cct.layout import dw
from cct.model import Snapshot, Sort
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
    ("\x0c", app.RELOAD),
    ("\x03", app.QUIT),
    (curses.KEY_RESIZE, app.RESIZE),
])
def test_key_table(key, kind):
    assert app.handle_key(state(), key).kind == kind


def test_ctrl_g_toggles_the_background_token():
    effect = app.handle_key(state(), "\x07")
    assert effect == app.Effect(app.TOGGLE_TOKEN, "bg:")


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


def test_counter_shows_shown_over_total():
    assert plain(app.counter_row(103, 122)) == "  103/122"


def test_counter_shows_scan_progress():
    row = app.counter_row(3, 3, Snapshot(scanning=True, scanned=120, total=500))
    assert plain(row) == "  3/3   scanning 120/500"


def test_header_switches_with_the_scope():
    assert plain(app.header_row("/home/user/project", False, 9)) == "  /home/user/project"
    assert plain(app.header_row("/home/user/project", True, 9)) == "  all projects (9)"


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


def test_footer_hides_the_branch_hint_outside_a_repo():
    assert "Ctrl+B" not in plain(app.footer_lines(200)[1])


def test_footer_names_the_scope_it_would_switch_to():
    assert "Ctrl+A all projects" in plain(app.footer_lines(200, all_projects=False)[1])
    assert "Ctrl+A this project" in plain(app.footer_lines(200, all_projects=True)[1])


def test_preview_header_labels_the_pane_and_counts_lines():
    label, indicator = app.preview_header("abcdef1234567890", 0, 74)
    assert label == "  abcdef12  "
    assert indicator == "1/74"
    assert app.preview_header("abcdef1234567890", 12, 74)[1] == "13/74"
