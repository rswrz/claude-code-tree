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
from .model import Session, Snapshot, TreeRow
from .theme import STATE_STYLE, Row, Style, plain

__all__ = [
    "dw", "dtrunc", "dpad", "fmt_age", "fmt_dur", "fmt_ctx",
    "build_rows", "plain_text",
    "NAME_MAX", "BR_MAX", "WT_MAX", "LINK_MAX", "PROJ_MAX", "LABEL_MIN", "MIN_WIDTH",
]

NAME_MAX = 60
BR_MAX = 20
WT_MAX = 16
LINK_MAX = 12
PROJ_MAX = 24
LABEL_MIN = 24
MIN_WIDTH = 38

# issue keys (ABC-123) and the two shapes a pull request is written in
KW_RE = re.compile(r"\bPR[ -]?\d+\b|#\d+|\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
PR_KW = re.compile(r"^(?:#\d+|PR[ -]?\d+)$")


def cwid(ch: str) -> int:
    if unicodedata.category(ch) == "Mn" or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def dw(s: str) -> int:
    """Display width in terminal cells."""
    return sum(cwid(c) for c in s)


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


def plain_text(row: Row) -> str:
    return plain(row)


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


def _badge(snap: Snapshot, sid: str) -> tuple[str, Style | None]:
    a = snap.agents.get(sid)
    if a is None:
        return "", None
    if a.kind == "background":
        text = f"bg:{a.state}" if a.state else "bg"
        if a.live:
            text = f"● {text}"
        return text, STATE_STYLE.get(a.state)
    if a.live:
        text = f"● live:{a.status}" if a.status else "● live"
        return text, STATE_STYLE.get(a.status, STATE_STYLE["live"])
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
    return _runs(cell, styles)


def _runs(text: str, styles: list[Style]) -> Row:
    out: Row = []
    for ch, st in zip(text, styles):
        if out and out[-1][1] is st:
            out[-1] = (out[-1][0] + ch, st)
        else:
            out.append((ch, st))
    return out


def build_rows(
    snap: Snapshot,
    tree_rows: list[TreeRow],
    width: int,
    show_project: bool,
) -> list[Row]:
    """One padded row per tree line, ready for the screen.

    ``width`` is the usable width of the list pane; every row comes back exactly
    that wide.
    """
    avail = max(MIN_WIDTH, width)
    order = [c for c in _ORDER if c != "project" or show_project]
    now = time.time()

    labels: list[tuple[str, str, str, int]] = []   # cell text, prefix, marker, depth
    table: list[dict[str, _Cell]] = []
    for tr in tree_rows:
        s = snap.sessions.get(tr.sid) or Session(sid=tr.sid)
        name = dtrunc(s.title, NAME_MAX)
        marker = " ⚠ no transcript" if s.ghost else (" ∅" if s.stub else "")
        label = f"{tr.prefix}{tr.connector}{name}"
        labels.append((label, tr.prefix, marker, tr.depth))

        proj = snap.project_of(tr.sid)
        btext, bstyle = _badge(snap, tr.sid)
        table.append({
            "label": _Cell(label + marker, "<", None, "label"),
            "project": _Cell(dtrunc(proj.name if proj else "", PROJ_MAX), "<", Style.PROJECT),
            "users": _Cell("" if s.stub or not s.users else f"{s.users}u", ">"),
            "age": _Cell("" if s.ghost else fmt_age(s.mtime, now), ">"),
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
        rows.append(_compact(out))
    return rows


def _compact(row: Row) -> Row:
    out: Row = []
    for text, st in row:
        if not text:
            continue
        if out and out[-1][1] is st:
            out[-1] = (out[-1][0] + text, st)
        else:
            out.append((text, st))
    return out
