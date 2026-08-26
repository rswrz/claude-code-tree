"""Modal boxes drawn over the list: confirm, text input, picker, message."""

from __future__ import annotations

import curses

from cct.layout import dw
from cct.theme import Row, Style, plain

from .screen import Screen, cut

MIN_INNER = 24
PAD = 2  # one border column plus one space, on each side

ALL = -1  # picker: the user asked for every item

BACKSPACE = ("\x7f", "\x08", curses.KEY_BACKSPACE)
ENTER = ("\r", curses.KEY_ENTER)
ESCAPE = ("\x1b", "\x03")


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def as_row(line: str | Row) -> Row:
    return [(line, Style.NORMAL)] if isinstance(line, str) else list(line)


def box_geometry(
    scr_h: int, scr_w: int, content_w: int, n_lines: int, title_w: int = 0
) -> tuple[int, int, int, int]:
    """(top, left, height, width) of a centred box that fits the screen."""
    inner = max(content_w, title_w, MIN_INNER)
    width = min(inner + 2 * PAD, max(scr_w, 1))
    height = min(n_lines + 2, max(scr_h, 1))
    top = max(0, (scr_h - height) // 2)
    left = max(0, (scr_w - width) // 2)
    return top, left, height, width


def delete_word(text: str) -> str:
    stripped = text.rstrip()
    idx = stripped.rfind(" ")
    return stripped[: idx + 1] if idx >= 0 else ""


def edit_line(text: str, key) -> tuple[str, bool]:
    """Apply one editing key. Returns the new text and whether it was consumed."""
    if key == "\x17":
        return delete_word(text), True
    if key == "\x15":
        return "", True
    if key in BACKSPACE:
        return text[:-1], True
    if isinstance(key, str) and len(key) == 1 and key.isprintable():
        return text + key, True
    return text, False


# --------------------------------------------------------------------------- #
# drawing
# --------------------------------------------------------------------------- #


def _draw_box(scr: Screen, title: str, body: list[Row]) -> tuple[int, int, int, int]:
    height, width = scr.size
    content_w = max([dw(plain(r)) for r in body] or [0])
    top, left, box_h, box_w = box_geometry(height, width, content_w, len(body), dw(title) + 2)
    inner_w = box_w - 2 * PAD
    win = scr.stdscr

    head = "┌" + "─" * (box_w - 2) + "┐"
    foot = "└" + "─" * (box_w - 2) + "┘"
    scr.draw_text(win, top, left, head, Style.BORDER, box_w)
    if title:
        scr.draw_text(win, top, left + 2, cut(f" {title} ", box_w - 4), Style.BOLD, box_w - 4)
    for i in range(1, box_h - 1):
        y = top + i
        scr.draw_text(win, y, left, "│" + " " * (box_w - 2) + "│", Style.BORDER, box_w)
        row = body[i - 1] if i - 1 < len(body) else []
        scr.draw_row(win, y, left + PAD, row, inner_w)
    scr.draw_text(win, top + box_h - 1, left, foot, Style.BORDER, box_w)
    return top, left, box_h, box_w


def _show(scr: Screen, title: str, lines: list[str | Row], extra: list[str | Row] | None = None):
    body = [as_row(ln) for ln in lines]
    if extra:
        body.append([])
        body.extend(as_row(ln) for ln in extra)
    scr.hide_cursor()
    geometry = _draw_box(scr, title, body)
    scr.refresh()
    return geometry, body


def _key(scr: Screen):
    while True:
        key = scr.get_key(-1)
        if key is not None:
            return key


# --------------------------------------------------------------------------- #
# widgets
# --------------------------------------------------------------------------- #


def message(scr: Screen, title: str, lines: list[str | Row]) -> None:
    _show(scr, title, lines, [[("any key to continue", Style.DIM)]])
    _key(scr)


def confirm(scr: Screen, title: str, lines: list[str | Row]) -> bool:
    _show(scr, title, lines, [[("[y/N] ", Style.BOLD)]])
    key = _key(scr)
    return isinstance(key, str) and key in ("y", "Y")


def text_input(scr: Screen, title: str, prompt: str, initial: str = "") -> str | None:
    text = initial
    while True:
        field: Row = [(prompt, Style.DIM), (text, Style.NORMAL)]
        (top, left, box_h, box_w), body = _show(scr, title, [field, [("Enter to confirm, Esc to cancel", Style.DIM)]])
        scr.place_cursor(top + 1, left + PAD + dw(prompt) + dw(text))
        scr.refresh()
        key = _key(scr)
        if key in ESCAPE:
            scr.hide_cursor()
            return None
        if key in ENTER:
            scr.hide_cursor()
            return text
        text, _handled = edit_line(text, key)


def picker(scr: Screen, title: str, items: list[str]) -> int | None:
    """Pick one of ``items``. Returns its index, :data:`ALL`, or None."""
    if not items:
        return None
    sel = 0
    hint = "1-%d, a for all, Esc cancels" % len(items)
    while True:
        body: list[Row] = []
        for i, item in enumerate(items):
            mark = "▌" if i == sel else " "
            style = Style.BOLD if i == sel else Style.NORMAL
            body.append([(mark, Style.GUTTER), (f" {i + 1}) ", Style.DIM), (item, style)])
        _show(scr, title, body, [[(hint, Style.DIM)]])
        key = _key(scr)
        if key in ESCAPE:
            return None
        if key in ENTER:
            return sel
        if key in ("a", "A"):
            return ALL
        if key in (curses.KEY_UP, "\x0b"):
            sel = (sel - 1) % len(items)
        elif key in (curses.KEY_DOWN, "\n"):
            sel = (sel + 1) % len(items)
        elif isinstance(key, str) and key.isdigit() and key != "0":
            index = int(key) - 1
            if index < len(items):
                return index
