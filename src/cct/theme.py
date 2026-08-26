"""Semantic styles and the 256-colour palette they resolve to.

Renderers emit ``Span`` tuples carrying a :class:`Style`; the curses layer is the
only place that knows about colour pairs and attributes.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class Style(Enum):
    NORMAL = "normal"
    DIM = "dim"
    BOLD = "bold"
    CHILD = "child"           # rows below the root of a tree
    TREE = "tree"             # the ├─ └─ │ skeleton
    ISSUE = "issue"           # issue keys (ABC-123) and ⧉ artifact tokens
    PR = "pr"                 # #353, "4 PRs"
    PR_KEYWORD = "pr_keyword"  # PR 142 inside a label
    BRANCH = "branch"         # branch column, off the default branch
    MARKER = "marker"         # ∅ / ⚠ no transcript
    PROJECT = "project"       # project column in all-projects scope
    STATE_OK = "state_ok"     # working, live
    STATE_WARN = "state_warn"  # blocked, waiting
    STATE_BAD = "state_bad"   # failed, stopped
    STATE_DONE = "state_done"  # done
    PROMPT = "prompt"         # "session >"
    COUNTER = "counter"       # "103/122"
    HEADER = "header"         # the cwd line
    GROUP = "group"           # group header splicing the session list
    FOOTER = "footer"         # key legend
    BORDER = "border"
    QUERY = "query"
    USER = "user"             # preview: user turns
    AGENT = "agent"           # preview: assistant turns
    HIT = "hit"               # preview: query match highlight
    GUTTER = "gutter"         # selection bar at column 0


class Ink(NamedTuple):
    fg: int          # 256-colour index, -1 = terminal default
    bold: bool = False
    dim: bool = False
    reverse: bool = False


PALETTE: dict[Style, Ink] = {
    Style.NORMAL: Ink(-1),
    Style.DIM: Ink(-1, dim=True),
    Style.BOLD: Ink(-1, bold=True),
    Style.CHILD: Ink(245),
    Style.TREE: Ink(-1),
    Style.ISSUE: Ink(179),
    Style.PR: Ink(110),
    Style.PR_KEYWORD: Ink(67),
    Style.BRANCH: Ink(108),
    Style.MARKER: Ink(131),
    Style.PROJECT: Ink(66),
    Style.STATE_OK: Ink(2),
    Style.STATE_WARN: Ink(3),
    Style.STATE_BAD: Ink(1),
    Style.STATE_DONE: Ink(-1, dim=True),
    Style.PROMPT: Ink(39, bold=True),
    Style.COUNTER: Ink(-1, dim=True),
    Style.HEADER: Ink(-1, dim=True),
    Style.GROUP: Ink(66, bold=True),
    Style.FOOTER: Ink(-1, bold=True),
    Style.BORDER: Ink(240),
    Style.QUERY: Ink(-1),
    Style.USER: Ink(6),
    Style.AGENT: Ink(2),
    Style.HIT: Ink(3, bold=True, reverse=True),
    Style.GUTTER: Ink(161),
}

# Background colour of the highlighted row (256-colour index, -1 = default).
SELECTED_BG = 236

STATE_STYLE: dict[str, Style] = {
    "working": Style.STATE_OK,
    "live": Style.STATE_OK,
    "blocked": Style.STATE_WARN,
    "waiting": Style.STATE_WARN,
    "failed": Style.STATE_BAD,
    "stopped": Style.STATE_BAD,
    "done": Style.STATE_DONE,
}

Span = tuple[str, Style]
Row = list[Span]


def plain(row: Row) -> str:
    return "".join(text for text, _ in row)


def merge(row: Row) -> Row:
    """Collapse neighbouring spans that share a style, dropping the empty ones."""
    out: Row = []
    for text, style in row:
        if not text:
            continue
        if out and out[-1][1] is style:
            out[-1] = (out[-1][0] + text, style)
        else:
            out.append((text, style))
    return out
