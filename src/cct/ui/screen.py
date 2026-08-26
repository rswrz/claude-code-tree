"""Curses initialisation, colour-pair allocation and span drawing.

This is the only place that knows about curses attributes; everything above it
speaks :class:`~cct.theme.Style`.
"""

from __future__ import annotations

import contextlib
import curses
import subprocess
from collections.abc import Callable, Iterator

from cct.layout import cwid, dw
from cct.theme import PALETTE, Ink, Row, Style


# --------------------------------------------------------------------------- #
# colour
# --------------------------------------------------------------------------- #

# xterm-256 cube and greyscale ramp collapse onto these when the terminal is
# limited to the eight ANSI colours.
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def basic_color(idx: int) -> int:
    """Nearest of the eight ANSI colours for a 256-colour index."""
    if idx < 0:
        return -1
    if idx < 8:
        return idx
    if idx < 16:
        return idx - 8
    if idx < 232:
        n = idx - 16
        r, g, b = _CUBE_LEVELS[n // 36], _CUBE_LEVELS[(n // 6) % 6], _CUBE_LEVELS[n % 6]
    else:
        level = 8 + (idx - 232) * 10
        r = g = b = level
    return (1 if r > 127 else 0) | (2 if g > 127 else 0) | (4 if b > 127 else 0)


class Palette:
    """Turns styles into curses attributes for whatever the terminal can do.

    Pairs are allocated on demand and never released; the cap keeps a
    pathological run from raising.
    """

    def __init__(
        self,
        colors: int,
        pairs: int,
        init_pair: Callable[[int, int, int], None],
    ) -> None:
        self.colors = colors
        self.pairs = pairs
        self._init_pair = init_pair
        self._cache: dict[tuple[int, int], int] = {}
        self._next = 1
        self.a_bold = curses.A_BOLD
        self.a_dim = curses.A_DIM
        self.a_reverse = curses.A_REVERSE

    @property
    def has_color(self) -> bool:
        return self.colors >= 8

    def color(self, idx: int) -> int:
        if idx < 0 or not self.has_color:
            return -1
        return idx if idx < self.colors else basic_color(idx)

    def pair_for(self, fg: int, bg: int) -> int:
        fg, bg = self.color(fg), self.color(bg)
        if not self.has_color or (fg < 0 and bg < 0):
            return 0
        key = (fg, bg)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if self._next >= self.pairs:
            return 0
        number = self._next
        try:
            self._init_pair(number, fg, bg)
        except Exception:
            self._cache[key] = 0
            return 0
        self._next += 1
        self._cache[key] = number
        return number

    def attr(self, style: Style, bg: int = -1) -> int:
        ink = PALETTE.get(style, Ink(-1))
        pair = self.pair_for(ink.fg, bg)
        attr = curses.color_pair(pair) if pair else 0
        if ink.bold:
            attr |= self.a_bold
        if ink.dim:
            attr |= self.a_dim
        if ink.reverse:
            attr |= self.a_reverse
        return attr


def cut(text: str, cells: int) -> str:
    """Hard clip to a cell budget — no ellipsis; the caller is out of room."""
    if cells <= 0:
        return ""
    if dw(text) <= cells:
        return text
    out: list[str] = []
    width = 0
    for ch in text:
        cw = cwid(ch)
        if width + cw > cells:
            break
        out.append(ch)
        width += cw
    return "".join(out)


# --------------------------------------------------------------------------- #
# escape sequences
# --------------------------------------------------------------------------- #

# How long to wait for the rest of a sequence once an ESC has arrived. Matches
# the escdelay the screen sets, so a solitary ESC still feels instant.
ESC_PEEK_MS = 25

# ncurses assembles arrow keys only while the terminal agrees with terminfo.
# Application cursor mode (smkx) yields SS3, but a terminal that ignores smkx —
# some tmux and screen configurations do — sends CSI, and ncurses then hands the
# bytes over one at a time. Both encodings have to end up as the same key.
_ARROWS = {
    "A": curses.KEY_UP,
    "B": curses.KEY_DOWN,
    "C": curses.KEY_RIGHT,
    "D": curses.KEY_LEFT,
    "H": curses.KEY_HOME,
    "F": curses.KEY_END,
}
_SHIFTED = {
    "A": curses.KEY_SR,
    "B": curses.KEY_SF,
    "C": curses.KEY_SRIGHT,
    "D": curses.KEY_SLEFT,
    "H": curses.KEY_SHOME,
    "F": curses.KEY_SEND,
}
_SS3 = dict(_ARROWS, P=curses.KEY_F1, Q=curses.KEY_F2, R=curses.KEY_F3, S=curses.KEY_F4)
_TILDE = {
    1: curses.KEY_HOME,
    2: curses.KEY_IC,
    3: curses.KEY_DC,
    4: curses.KEY_END,
    5: curses.KEY_PPAGE,
    6: curses.KEY_NPAGE,
    7: curses.KEY_HOME,
    8: curses.KEY_END,
}
_TILDE_SHIFTED = {5: curses.KEY_SPREVIOUS, 6: curses.KEY_SNEXT, 3: curses.KEY_SDC}

SHIFT = 2  # xterm modifier parameter: 1 + 1 for shift


def _params(raw: str) -> tuple[int | None, int | None]:
    parts = raw.split(";")
    def number(i: int) -> int | None:
        try:
            return int(parts[i])
        except (IndexError, ValueError):
            return None
    return number(0), number(1)


def _read_csi(next_char) -> tuple[str, str] | None:
    """Collect a CSI body: parameter bytes, intermediates, then the final byte."""
    raw: list[str] = []
    ch = next_char()
    while isinstance(ch, str) and len(ch) == 1 and "\x30" <= ch <= "\x3f":
        raw.append(ch)
        ch = next_char()
    while isinstance(ch, str) and len(ch) == 1 and "\x20" <= ch <= "\x2f":
        ch = next_char()
    if not isinstance(ch, str) or len(ch) != 1 or not ("\x40" <= ch <= "\x7e"):
        return None
    return "".join(raw), ch


def decode_escape(next_char):
    """Turn an ESC and whatever follows it into the key ncurses would report.

    ``next_char`` reads one more character without blocking, returning None when
    nothing follows. Anything that parses as a sequence but means nothing here is
    swallowed (None) rather than leaking its tail into the query.
    """
    ch = next_char()
    if ch is None:
        return "\x1b"
    if not isinstance(ch, str):
        return ch          # ncurses assembled a key after all
    if ch == "O":
        final = next_char()
        return _SS3.get(final) if isinstance(final, str) else None
    if ch != "[":
        return None        # Alt+key; nothing here binds it
    body = _read_csi(next_char)
    if body is None:
        return None
    raw, final = body
    first, modifier = _params(raw)
    if final == "~":
        table = _TILDE_SHIFTED if modifier == SHIFT else _TILDE
        return table.get(first) if first is not None else None
    table = _SHIFTED if modifier == SHIFT else _ARROWS
    return table.get(final)


# --------------------------------------------------------------------------- #
# screen
# --------------------------------------------------------------------------- #


class Screen:
    def __init__(self, stdscr) -> None:
        self.stdscr = stdscr
        colors = pairs = 0
        try:
            curses.start_color()
            curses.use_default_colors()
            colors, pairs = curses.COLORS, curses.COLOR_PAIRS
        except curses.error:
            pass
        self.palette = Palette(colors, pairs, curses.init_pair)
        self._setup()

    def _setup(self) -> None:
        curses.raw()
        curses.noecho()
        curses.nonl()
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        self.stdscr.keypad(True)
        with contextlib.suppress(AttributeError, curses.error):
            curses.set_escdelay(25)
        with contextlib.suppress(curses.error):
            curses.mousemask(0)

    # -- geometry ----------------------------------------------------------- #

    @property
    def size(self) -> tuple[int, int]:
        return self.stdscr.getmaxyx()

    @property
    def width(self) -> int:
        return self.stdscr.getmaxyx()[1]

    # -- drawing ------------------------------------------------------------ #

    def attr(self, style: Style, bg: int = -1) -> int:
        return self.palette.attr(style, bg)

    def fill(self, win, y: int, x: int, width: int, bg: int) -> None:
        if width <= 0:
            return
        pair = self.palette.pair_for(-1, bg)
        with contextlib.suppress(curses.error):
            win.addstr(y, x, " " * width, curses.color_pair(pair) if pair else 0)

    def draw_row(self, win, y: int, x: int, row: Row, width: int, bg: int = -1,
                 bold: bool = False) -> None:
        """Draw a row of spans, clipping at ``width``. ``bg`` paints the line."""
        if width <= 0:
            return
        if bg >= 0:
            self.fill(win, y, x, width, bg)
        extra = self.palette.a_bold if bold else 0
        cur = x
        limit = x + width
        for text, style in row:
            if cur >= limit:
                break
            piece = cut(text, limit - cur)
            if not piece:
                continue
            with contextlib.suppress(curses.error):
                win.addstr(y, cur, piece, self.attr(style, bg) | extra)
            cur += dw(piece)

    def draw_text(self, win, y: int, x: int, text: str, style: Style, width: int,
                  bg: int = -1, bold: bool = False) -> None:
        self.draw_row(win, y, x, [(text, style)], width, bg, bold)

    def erase(self) -> None:
        self.stdscr.erase()

    def refresh(self) -> None:
        self.stdscr.noutrefresh()
        curses.doupdate()

    def place_cursor(self, y: int, x: int) -> None:
        height, width = self.size
        with contextlib.suppress(curses.error):
            curses.curs_set(1)
            self.stdscr.move(min(y, height - 1), min(x, width - 1))

    def hide_cursor(self) -> None:
        with contextlib.suppress(curses.error):
            curses.curs_set(0)

    # -- input -------------------------------------------------------------- #

    def get_key(self, timeout_ms: int = -1):
        """One key, or None when the timeout expired."""
        self.stdscr.timeout(timeout_ms)
        key = self._read()
        if key != "\x1b":
            return key
        self.stdscr.timeout(ESC_PEEK_MS)
        try:
            return decode_escape(self._read)
        finally:
            self.stdscr.timeout(timeout_ms)

    def _read(self):
        try:
            return self.stdscr.get_wch()
        except curses.error:
            return None
        except KeyboardInterrupt:
            return "\x03"

    # -- handing the terminal over ------------------------------------------ #

    @contextlib.contextmanager
    def suspend(self) -> Iterator[None]:
        """Give the real terminal to a child process, then take it back.

        The restore runs from a ``finally``, so a child that dies, an exception
        or a Ctrl+C inside the child all still land back in a working UI.
        """
        self.stdscr.clear()
        self.stdscr.refresh()
        with contextlib.suppress(curses.error):
            curses.def_prog_mode()
        curses.endwin()
        try:
            yield
        finally:
            with contextlib.suppress(curses.error):
                curses.reset_prog_mode()
            self._setup()
            with contextlib.suppress(curses.error):
                curses.flushinp()
            self.stdscr.clearok(True)
            self.stdscr.refresh()
            curses.doupdate()

    def run(self, argv: list[str], cwd: str | None = None) -> int:
        """Run a command on the real terminal and return its exit status."""
        with self.suspend():
            try:
                return subprocess.call(argv, cwd=cwd or None)
            except FileNotFoundError:
                return 127
            except KeyboardInterrupt:
                return 130
            except OSError:
                return 1


def launch(main: Callable[[Screen], int]) -> int:
    """curses.wrapper, handing the callable a :class:`Screen`."""
    return curses.wrapper(lambda stdscr: main(Screen(stdscr)))
