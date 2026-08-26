"""Query matching, following fzf's extended-search grammar in ``--exact`` mode.

Implements the four non-fuzzy match algorithms plus the fuzzy one that ``'``
switches on.

Query grammar::

    term term      both must match (AND)
    term | term    either may match (OR, within one group)
    'term          fuzzy (subsequence) instead of substring
    ^term          anchored at the start
    term$          anchored at the end
    ^term$         the whole haystack
    !term          must not match; combines with ^ and $

Case is smart: a term written in lower case matches case-insensitively, any upper
case letter in it makes the whole term case-sensitive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = ["TermType", "Term", "Query", "parse", "matches", "spans", "toggle_token"]

# fzf splits the query on runs of plain spaces; the no-break space inside a
# filter token therefore never breaks it into two terms.
_SPLIT = re.compile(" +")


class TermType(Enum):
    FUZZY = "fuzzy"
    EXACT = "exact"
    PREFIX = "prefix"
    SUFFIX = "suffix"
    EQUAL = "equal"


@dataclass(frozen=True)
class Term:
    typ: TermType
    inv: bool
    text: str
    case_sensitive: bool

    def key(self) -> str:
        return self.text if self.case_sensitive else self.text.lower()


@dataclass(frozen=True)
class Query:
    """Groups of OR-ed terms; the groups themselves are AND-ed."""

    sets: tuple[tuple[Term, ...], ...] = ()
    raw: str = ""

    def __bool__(self) -> bool:
        return bool(self.sets)

    @property
    def terms(self) -> list[Term]:
        return [t for group in self.sets for t in group]

    @property
    def positive(self) -> list[Term]:
        return [t for t in self.terms if not t.inv]


def parse(query: str) -> Query:
    escaped = query.replace("\\ ", "\t")
    sets: list[tuple[Term, ...]] = []
    group: list[Term] = []
    switch = False
    after_bar = False

    for token in _SPLIT.split(escaped):
        text = token.replace("\t", " ")
        lower = text.lower()
        case_sensitive = text != lower
        if not case_sensitive:
            text = lower
        typ = TermType.EXACT
        inv = False

        if group and not after_bar and text == "|":
            switch = False
            after_bar = True
            continue
        after_bar = False

        if text.startswith("!"):
            inv = True
            text = text[1:]

        if text != "$" and text.endswith("$"):
            typ = TermType.SUFFIX
            text = text[:-1]

        if text.startswith("'"):
            # --exact makes bare terms exact, so the quote turns them fuzzy again
            typ = TermType.FUZZY
            text = text[1:]
        elif text.startswith("^"):
            typ = TermType.EQUAL if typ is TermType.SUFFIX else TermType.PREFIX
            text = text[1:]

        if not text:
            continue
        if switch:
            sets.append(tuple(group))
            group = []
        group.append(Term(typ, inv, text, case_sensitive))
        switch = True

    if group:
        sets.append(tuple(group))
    return Query(tuple(sets), query)


def matches(q: Query, haystack: str) -> bool:
    if not q.sets:
        return True
    for group in q.sets:
        ok = False
        for term in group:
            if _find(term, haystack) is not None:
                if term.inv:
                    continue
                ok = True
                break
            if term.inv:
                ok = True
        if not ok:
            return False
    return True


def spans(q: Query, text: str) -> list[tuple[int, int]]:
    """Every occurrence of every positive term, merged and ordered.

    The preview paints all of them; fzf itself only scores the one best span.
    """
    found: list[tuple[int, int]] = []
    for term in q.positive:
        found.extend(_occurrences(term, text))
    return _merge(found)


def toggle_token(query: str, token: str) -> str:
    """Add the token to the query, or drop it if it is already there."""
    fields = [f for f in query.split(" ") if f]
    if token:
        fields = [f for f in fields if f != token] if token in fields else fields + [token]
    return " ".join(fields)


def _leading(s: str) -> int:
    i = 0
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _trailing(s: str) -> int:
    i = 0
    while i < len(s) and s[len(s) - 1 - i].isspace():
        i += 1
    return i


def _find(term: Term, text: str) -> tuple[int, int] | None:
    """First match of a single term, as a half-open range, or None."""
    hay = text if term.case_sensitive else text.lower()
    pat = term.key()
    if not pat:
        return None

    if term.typ is TermType.EXACT:
        i = hay.find(pat)
        return (i, i + len(pat)) if i >= 0 else None

    if term.typ is TermType.PREFIX:
        start = 0 if pat[0].isspace() else _leading(hay)
        return (start, start + len(pat)) if hay.startswith(pat, start) else None

    if term.typ is TermType.SUFFIX:
        end = len(hay) - (0 if pat[-1].isspace() else _trailing(hay))
        return (end - len(pat), end) if end >= len(pat) and hay.endswith(pat, 0, end) else None

    if term.typ is TermType.EQUAL:
        start = 0 if pat[0].isspace() else _leading(hay)
        end = len(hay) - (0 if pat[-1].isspace() else _trailing(hay))
        return (start, end) if hay[start:end] == pat else None

    return _fuzzy(hay, pat)


def _fuzzy(hay: str, pat: str) -> tuple[int, int] | None:
    """Tightest forward subsequence match, fzf-style: scan forward, then shrink."""
    j = 0
    first = -1
    for i, ch in enumerate(hay):
        if ch == pat[j]:
            if first < 0:
                first = i
            j += 1
            if j == len(pat):
                end = i + 1
                break
    else:
        return None
    j = len(pat) - 1
    for i in range(end - 1, first - 1, -1):
        if hay[i] == pat[j]:
            j -= 1
            if j < 0:
                return (i, end)
    return (first, end)


def _occurrences(term: Term, text: str) -> list[tuple[int, int]]:
    hay = text if term.case_sensitive else text.lower()
    pat = term.key()
    if not pat:
        return []
    if term.typ is TermType.EXACT:
        out = []
        i = hay.find(pat)
        while i >= 0:
            out.append((i, i + len(pat)))
            i = hay.find(pat, i + 1)
        return out
    if term.typ is TermType.FUZZY:
        window = _fuzzy(hay, pat)
        if window is None:
            return []
        out = []
        j = 0
        for i in range(window[0], window[1]):
            if j < len(pat) and hay[i] == pat[j]:
                out.append((i, i + 1))
                j += 1
        return out
    hit = _find(term, text)
    return [hit] if hit else []


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out
