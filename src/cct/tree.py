"""Reconstructing the fork forest from a flat pile of transcripts.

A fork copies its parent's records verbatim, so two sessions sharing a long
prefix of record uuids are almost certainly parent and child. That inference is
the fallback; explicit evidence from the daemon or the job state wins.
"""

from __future__ import annotations

from .model import Session, Sort, TreeRow

CONNECTOR_LAST = "└─ "
CONNECTOR_MID = "├─ "
PREFIX_LAST = "   "
PREFIX_MID = "│  "


def _lcp(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def lcp_parents(sessions: dict[str, Session]) -> dict[str, str | None]:
    """For each session, the oldest earlier session sharing the longest uuid prefix.

    Only sessions whose *first* uuid matches can share any prefix at all, which
    turns the pairwise scan into a lookup over one small fork group.
    """
    order = sorted(sessions, key=lambda sid: sessions[sid].mtime)
    groups: dict[str, list[str]] = {}
    parents: dict[str, str | None] = {}
    for sid in order:
        uuids = sessions[sid].uuids
        best_lcp, best = 0, None
        if uuids:
            group = groups.setdefault(uuids[0], [])
            for other in group:
                length = _lcp(uuids, sessions[other].uuids)
                if length > best_lcp:
                    best_lcp, best = length, other
            group.append(sid)
        parents[sid] = best
    return parents


def compute_parents(
    sessions: dict[str, Session],
    jobs: dict[str, dict],
    roster_parents: dict[str, str],
) -> dict[str, str | None]:
    """Resolve one parent per session and break any cycle that survives it."""
    inferred = lcp_parents(sessions)

    by_title: dict[str, set[str]] = {}
    for sid, session in sessions.items():
        for title in (session.custom, session.ai):
            if title:
                by_title.setdefault(title, set()).add(sid)

    def resolve(sid: str) -> str | None:
        candidate = roster_parents.get(sid)
        if candidate in sessions and candidate != sid:
            return candidate
        candidate = (jobs.get(sid) or {}).get("forkParentSessionId")
        if isinstance(candidate, str) and candidate in sessions and candidate != sid:
            return candidate
        candidate = inferred.get(sid)
        if candidate and candidate != sid:
            return candidate
        if sessions[sid].count == 0:
            # a transcript-less job still names itself; one unambiguous namesake adopts it
            name = (jobs.get(sid) or {}).get("name")
            if isinstance(name, str) and name:
                candidates = sorted(by_title.get(name, set()) - {sid})
                if len(candidates) == 1:
                    return candidates[0]
        return None

    parents = {sid: resolve(sid) for sid in sorted(sessions)}

    for sid in sorted(sessions):
        seen: set[str] = set()
        parent = parents.get(sid)
        while parent:
            if parent == sid:
                parents[sid] = None
                break
            if parent in seen:
                break
            seen.add(parent)
            parent = parents.get(parent)
    return parents


def _subtree_mtime(
    sessions: dict[str, Session],
    children: dict[str, list[str]],
    roots: list[str],
) -> dict[str, float]:
    """A branch ranks by its most recent session, so touching a leaf lifts its whole tree."""
    recent: dict[str, float] = {}
    for root in roots:
        stack = [(root, False)]
        while stack:
            sid, done = stack.pop()
            if done:
                value = sessions[sid].mtime
                for child in children[sid]:
                    value = max(value, recent[child])
                recent[sid] = value
                continue
            stack.append((sid, True))
            for child in children[sid]:
                stack.append((child, False))
    return recent


def _own_key(session: Session, sort: Sort) -> float:
    if sort is Sort.TURNS:
        return float(session.users)
    if sort is Sort.CONTEXT:
        return float(session.ctx or 0)
    if sort is Sort.DURATION:
        if session.ts_min is None or session.ts_max is None:
            return 0.0
        return (session.ts_max - session.ts_min).total_seconds()
    return session.mtime


def order_forest(
    sessions: dict[str, Session],
    parents: dict[str, str | None],
    sort: Sort = Sort.RECENT,
) -> tuple[list[str], dict[str, list[str]]]:
    """Attach every session to its parent, then order roots and siblings alike."""
    children: dict[str, list[str]] = {sid: [] for sid in sessions}
    roots: list[str] = []
    for sid in sorted(sessions):
        parent = parents.get(sid)
        (children[parent] if parent in children else roots).append(sid)

    if sort is Sort.NAME:
        def key(sid: str) -> tuple:
            return (sessions[sid].title.casefold(), sid)
        reverse = False
    elif sort is Sort.RECENT:
        recent = _subtree_mtime(sessions, children, roots)
        def key(sid: str) -> tuple:
            return (recent[sid], sid)
        reverse = True
    else:
        def key(sid: str) -> tuple:
            return (_own_key(sessions[sid], sort), sid)
        reverse = True

    roots.sort(key=key, reverse=reverse)
    for kids in children.values():
        kids.sort(key=key, reverse=reverse)
    return roots, children


def build_forest(
    sessions: dict[str, Session],
    jobs: dict[str, dict],
    roster_parents: dict[str, str],
    sort: Sort = Sort.RECENT,
) -> tuple[list[str], dict[str, list[str]], dict[str, str | None]]:
    parents = compute_parents(sessions, jobs, roster_parents)
    roots, children = order_forest(sessions, parents, sort)
    return roots, children, parents


def walk(roots: list[str], children: dict[str, list[str]]) -> list[TreeRow]:
    """Depth-first display order, carrying the skeleton each row draws itself with."""
    rows: list[TreeRow] = []
    # pushed back-to-front so the stack pops the roots in order; the last root, pushed
    # first, is the one that draws the closing connector
    stack = [(sid, "", i == 0, 0) for i, sid in enumerate(reversed(roots))]
    while stack:
        sid, prefix, is_last, depth = stack.pop()
        rows.append(TreeRow(
            sid=sid,
            prefix=prefix,
            connector=CONNECTOR_LAST if is_last else CONNECTOR_MID,
            depth=depth,
        ))
        kids = children.get(sid) or []
        child_prefix = prefix + (PREFIX_LAST if is_last else PREFIX_MID)
        for i, kid in enumerate(reversed(kids)):
            stack.append((kid, child_prefix, i == 0, depth + 1))
    return rows
