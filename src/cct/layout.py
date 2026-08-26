"""Column layout and colouring of the session list.

Turns the forest walk into one padded :data:`~cct.theme.Row` per line, dropping
the least load-bearing columns first when the terminal is narrow.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from .daemon import job_prs
from .group import partition
from .model import Agent, Group, Session, Snapshot, TreeRow
from .theme import STATE_STYLE, Row, Style, merge, plain
from .tree import walk

__all__ = [
    "cwid", "dw", "dtrunc", "dpad", "fmt_age", "fmt_dur", "fmt_ctx",
    "build_rows", "build_list", "ListRows", "group_header", "KW_RE", "PR_KW",
    "LABEL_MIN", "MIN_WIDTH", "BR_MAX", "SETTLED", "badge", "stalled",
]

NAME_MAX = 60
BR_MAX = 20
WT_MAX = 16
LINK_MAX = 12
PROJ_MAX = 24
LABEL_MIN = 24
MIN_WIDTH = 38
HEADER_INDENT = 2

# background states that no longer expect a worker process; every other state
# means the job claims to be running
SETTLED = frozenset({"blocked", "failed", "done", "stopped"})

# issue keys (ABC-123) and the two shapes a pull request is written in
KW_RE = re.compile(r"\bPR[ -]?\d+\b|#\d+|\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
PR_KW = re.compile(r"^(?:#\d+|PR[ -]?\d+)$")


# Every list row and every preview line is measured character by character, and
# the three unicodedata lookups are the expensive part; one entry per distinct
# character keeps the draw loop out of them.
_CWID: dict[str, int] = {}


def cwid(ch: str) -> int:
    """Display width of one character in terminal cells."""
    width = _CWID.get(ch)
    if width is None:
        if unicodedata.category(ch) == "Mn" or unicodedata.combining(ch):
            width = 0
        else:
            width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        _CWID[ch] = width
    return width


def dw(s: str) -> int:
    """Display width in terminal cells."""
    if s.isascii():
        return len(s)
    total = 0
    get = _CWID.get
    for ch in s:
        width = get(ch)
        total += cwid(ch) if width is None else width
    return total


def dtrunc(s: str, n: int) -> str:
    if n <= 0:
        return ""
    if dw(s) <= n:
        return s
    out: list[str] = []
    w = 0
    for ch in s:
        c = cwid(ch)
        if w + c > n - 1:
            break
        out.append(ch)
        w += c
    return "".join(out) + "…"


def dpad(s: str, n: int, align: str) -> str:
    p = " " * max(0, n - dw(s))
    return s + p if align == "<" else p + s


def fmt_age(mtime: float, now: float | None = None) -> str:
    diff = (time.time() if now is None else now) - mtime
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    if diff < 86400 * 30:
        return f"{int(diff / 86400)}d ago"
    if diff < 86400 * 365:
        return f"{int(diff / 86400 / 30)}mo ago"
    return f"{int(diff / 86400 / 365)}y ago"


def fmt_dur(a: datetime | None, b: datetime | None) -> str:
    if a is None or b is None or a == b:
        return ""
    secs = (b - a).total_seconds()
    if secs < 60:
        return "<1m"
    if secs < 3600:
        return f"{int(secs / 60)}m"
    if secs < 86400:
        return f"{int(secs / 3600)}h{int(secs % 3600 / 60)}m"
    return f"{int(secs / 86400)}d{int(secs % 86400 / 3600)}h"


def fmt_ctx(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.0f}k"


@dataclass
class _Cell:
    text: str
    align: str = "<"
    style: Style | None = None
    kind: str = ""


# Left to right. 'project' only appears in all-projects scope.
_ORDER = ["label", "project", "users", "age", "duration", "context", "branch", "links", "badge"]
# Narrow terminals give up the least load-bearing columns first.
_DROP = ["duration", "context", "branch", "age"]



def _branch_tok(s: Session) -> str:
    # no-break space: renders as a gap, but keeps ctrl-b's filter token a single term
    tok = "⎇ " + dtrunc(s.branch, BR_MAX - 2) if s.branch else ""
    if s.worktree:
        wt = f"[{dtrunc(s.worktree, WT_MAX - 2)}]"
        tok = f"{tok} {wt}" if tok else wt
    return tok


def _link_tok(s: Session, job: dict) -> str:
    nums = sorted(s.prs | job_prs(job))
    parts = []
    if s.frames:
        parts.append("⧉ " + str(s.frames) if s.frames > 1 else "⧉")
    if len(nums) == 1:
        parts.append(f"#{nums[0]}")
    elif nums:
        parts.append(f"{len(nums)} PRs")
    return dtrunc(" ".join(parts), LINK_MAX)


def stalled(agent: Agent) -> bool:
    """A background state that claims work with no worker behind it."""
    return bool(agent.state) and agent.state not in SETTLED and not agent.live


def badge(agent: Agent | None) -> tuple[str, Style | None]:
    """One agent's badge: the text, and the colour that carries liveness.

    The token always spells its colon, so every badge matches the query token
    that is meant to find it. A stalled job is a broken one rather than a busy
    one, and the colour is the only place that shows.
    """
    if agent is None:
        return "", None
    if agent.kind == "background":
        text = f"bg:{agent.state}"
        if stalled(agent):
            return text, Style.STATE_BAD
        return text, STATE_STYLE.get(agent.state)
    if agent.live:
        return f"live:{agent.status}", STATE_STYLE.get(agent.status, STATE_STYLE["live"])
    return "", None


def _off_default(s: Session, default_branch: str) -> Style | None:
    # work happening away from the default branch is worth spotting at a glance
    if not default_branch or not s.branch:
        return None
    return Style.BRANCH if s.branch != default_branch else None


def _link_spans(text: str, base: Style) -> Row:
    """Colour the links cell.

    A bare number belongs to the PR token when 'PRs' follows it, to the artifact
    count otherwise.
    """
    toks = text.split(" ")
    out: Row = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if t == "⧉":
            if nxt.isdigit() and (toks[i + 2] if i + 2 < len(toks) else "") != "PRs":
                t = f"⧉ {nxt}"
                i += 1
            out.append((t, Style.ISSUE))
        elif t.isdigit() and nxt == "PRs":
            out.append((f"{t} PRs", Style.PR))
            i += 1
        elif t.startswith("#"):
            out.append((t, Style.PR))
        else:
            out.append((t, base))
        out.append((" ", base))
        i += 1
    if out:
        out.pop()
    return out


def _label_spans(cell: str, prefix: str, marker: str, base: Style) -> Row:
    styles: list[Style] = [base] * len(cell)
    for m in KW_RE.finditer(cell):
        st = Style.PR_KEYWORD if PR_KW.match(m.group(0)) else Style.ISSUE
        for i in range(m.start(), m.end()):
            styles[i] = st
    if marker:
        i = cell.find(marker)
        if i >= 0:
            for j in range(i, i + len(marker)):
                styles[j] = Style.MARKER
    # the tree skeleton belongs to the level above, so it keeps the default colour
    if prefix and cell.startswith(prefix):
        for i in range(len(prefix)):
            styles[i] = Style.TREE
    # one span per character; build_rows merges the whole row in a single pass
    return list(zip(cell, styles))


def build_rows(
    snap: Snapshot,
    tree_rows: list[TreeRow],
    width: int,
    show_project: bool,
    now: float | None = None,
) -> list[Row]:
    """One padded row per tree line, ready for the screen.

    ``width`` is the usable width of the list pane; every row comes back exactly
    that wide. ``now`` is the clock the age column reads.
    """
    avail = max(MIN_WIDTH, width)
    order = [c for c in _ORDER if c != "project" or show_project]
    clock = time.time() if now is None else now

    labels: list[tuple[str, str, str, int]] = []   # cell text, prefix, marker, depth
    table: list[dict[str, _Cell]] = []
    for tr in tree_rows:
        s = snap.sessions.get(tr.sid) or Session(sid=tr.sid)
        name = dtrunc(s.title, NAME_MAX)
        marker = " ⚠ no transcript" if s.ghost else (" ∅" if s.stub else "")
        label = f"{tr.prefix}{tr.connector}{name}"
        labels.append((label, tr.prefix, marker, tr.depth))

        proj = snap.project_of(tr.sid)
        btext, bstyle = badge(snap.agents.get(tr.sid))
        table.append({
            "label": _Cell(label + marker, "<", None, "label"),
            "project": _Cell(dtrunc(proj.name if proj else "", PROJ_MAX), "<", Style.PROJECT),
            "users": _Cell("" if s.stub or not s.users else f"{s.users}u", ">"),
            "age": _Cell("" if s.ghost else fmt_age(s.mtime, clock), ">"),
            "duration": _Cell("" if s.stub else fmt_dur(s.ts_min, s.ts_max), ">"),
            "context": _Cell("" if s.stub else fmt_ctx(s.ctx), ">"),
            "branch": _Cell(_branch_tok(s), "<", _off_default(s, snap.default_branch)),
            "links": _Cell(_link_tok(s, snap.jobs.get(tr.sid) or {}), "<", None, "link"),
            "badge": _Cell(btext, "<", bstyle),
        })

    widths = {c: max((dw(r[c].text) for r in table), default=0) for c in order}
    keep = [c for c in order if widths[c] > 0]
    if "label" not in keep:
        keep.insert(0, "label")

    lw = avail
    di = 0
    while True:
        others = sum(widths[c] for c in keep if c != "label")
        lw = avail - others - 2 * (len(keep) - 1)
        if lw >= LABEL_MIN or di >= len(_DROP):
            break
        drop = _DROP[di]
        di += 1
        keep = [c for c in keep if c != drop]
    widths["label"] = max(1, min(widths["label"], lw))
    last = keep[-1]

    rows: list[Row] = []
    for (label, prefix, marker, depth), row in zip(labels, table):
        # row colour tracks tree depth only; a stub is called out by its marker instead
        base = Style.CHILD if depth > 0 else Style.NORMAL
        cells: list[Row] = []
        for c in keep:
            cell = row[c]
            text, w = cell.text, widths[c]
            if c == "label":
                mw = dw(marker)
                text = dtrunc(text, w) if mw >= w else dtrunc(label, w - mw) + marker
            if cell.kind == "link":
                # colour before padding: the token walker must not see the filler
                pad = "" if c == last else " " * max(0, w - dw(text))
                spans = _link_spans(text, base)
                if pad:
                    spans.append((pad, base))
            else:
                padded = dpad(text, w, cell.align)
                if c == last:
                    padded = padded.rstrip()
                if cell.kind == "label":
                    spans = _label_spans(padded, prefix, marker, base)
                else:
                    spans = [(padded, cell.style or base)] if padded else []
            cells.append(spans)

        out: Row = []
        for i, spans in enumerate(cells):
            if i:
                out.append(("  ", base))
            out.extend(spans)
        fill = avail - dw(plain(out))
        if fill > 0:
            out.append((" " * fill, base))
        rows.append(merge(out))
    return rows


@dataclass
class ListRows:
    rows: list[Row]
    sids: list[str]           # '' for a group header
    labels: list[str]         # the block each row belongs to, '' when unblocked


def group_header(label: str, n: int, width: int) -> Row:
    """One block header, as wide as the session rows beside it.

    ``width`` is the usable width of the list pane, clamped the way
    :func:`build_rows` clamps it.
    """
    width = max(MIN_WIDTH, width)
    lead = " " * HEADER_INDENT
    count = f" ({n})"
    # a space stays between the count and the rule, whatever the label costs
    text = lead + dtrunc(label, max(0, width - len(lead) - dw(count) - 1))
    fill = max(0, width - dw(text) - dw(count) - 1)
    return merge([
        (text, Style.GROUP),
        (count, Style.COUNTER),
        (" " + "─" * fill, Style.BORDER),
    ])


def build_list(
    snap: Snapshot,
    roots: list[str],
    children: dict[str, list[str]],
    width: int,
    show_project: bool,
    group: Group = Group.NONE,
    now: float | None = None,
) -> ListRows:
    """The whole session list: group headers spliced into the forest walk.

    ``rows``, ``sids`` and ``labels`` come back the same length, with ``''`` in
    ``sids`` at every header position and every row's block label in ``labels``;
    selection and filtering rely on that 1:1 correspondence.
    """
    avail = max(MIN_WIDTH, width)
    parts = partition(snap, roots, children, group, now)
    # a header over the whole list names nothing the list does not already say
    lone = len(parts) == 1
    # the label column earns the space the project column would waste on a value
    # the header states — but only where a header is drawn to state it
    if group is Group.PROJECT and not lone:
        show_project = False
    blocks: list[tuple[str, list[TreeRow]]] = []
    for block in parts:
        # walked per block, so each block's last root draws its own closing connector
        blocks.append((block.label, walk(block.roots, children)))

    tree_rows = [tr for _, walked in blocks for tr in walked]
    # one call, so the column widths stay aligned across every block
    body = build_rows(snap, tree_rows, avail, show_project, now)

    rows: list[Row] = []
    sids: list[str] = []
    labels: list[str] = []
    at = 0
    for label, walked in blocks:
        if label and not lone:
            rows.append(group_header(label, len(walked), avail))
            sids.append("")
            labels.append(label)
        rows.extend(body[at:at + len(walked)])
        sids.extend(tr.sid for tr in walked)
        labels.extend(label for _ in walked)
        at += len(walked)
    return ListRows(rows=rows, sids=sids, labels=labels)
