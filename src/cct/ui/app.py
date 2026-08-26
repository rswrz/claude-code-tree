"""The list screen: state, key handling, drawing and the main loop.

Key handling is a pure function over :class:`UIState` returning an
:class:`Effect`, so the interaction table is testable without a terminal.
Side effects all live in :class:`App`.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass

from cct import actions, fuzzy, gitinfo, layout, paths, preview, store
from cct.model import Agent, Group, Session, Snapshot, Sort
from cct.theme import SELECTED_BG, STATE_STYLE, Row, Style, plain

from . import widgets
from .screen import Screen, launch

PROMPT = "session > "
PREVIEW_FRACTION = 0.55
POLL_SECS = 0.5
TICK_MS = 500
GUTTER = "▌"

# The branch filter token carries a no-break space so fzf-style parsing keeps it
# a single term; the branch column is rendered the same way.
BRANCH_TOKEN = "\u2387\u00a0"

# Ctrl+G walks these in turn and then out again: any background agent first, the
# states worth singling out, then the interactive sessions with a worker on them.
AGENT_CYCLE = ("bg:", "bg:blocked", "bg:failed", "bg:done", "live:")


# --------------------------------------------------------------------------- #
# effects
# --------------------------------------------------------------------------- #

NONE = "none"
QUIT = "quit"
MOVE = "move"
QUERY = "query"
PREVIEW = "preview"
RESIZE = "resize"
RELOAD = "reload"
RESUME = "resume"
FORK = "fork"
NEW = "new"
DISPATCH = "dispatch"
RENAME = "rename"
DELETE = "delete"
PRUNE = "prune"
LINKS = "links"
TOGGLE_TOKEN = "toggle_token"
TOGGLE_SCOPE = "toggle_scope"
CYCLE_SORT = "cycle_sort"
CYCLE_GROUP = "cycle_group"
CYCLE_AGENT = "cycle_agent"


@dataclass(frozen=True)
class Effect:
    kind: str = NONE
    arg: str = ""


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Tally:
    """What the session rows on screen are: the status line's groups and slots.

    The two group counts split ``shown`` between them and the four background
    slots split ``background``, both by construction. Every background agent
    holds a row of its own, a ghost one where the scope found no transcript.
    """

    interactive: int = 0
    live: int = 0              # interactive rows with a worker behind them
    background: int = 0
    running: int = 0
    blocked: int = 0
    bad: int = 0               # failed, stopped, and work with no worker behind it
    done: int = 0


def _tally(sids: list[str], agents: dict[str, Agent]) -> Tally:
    """Sort session rows into the two groups and the four background slots."""
    interactive = live = background = running = blocked = bad = 0
    for sid in sids:
        agent = agents.get(sid)
        if agent is not None and agent.kind == "background":
            background += 1
            if agent.state == "blocked":
                blocked += 1
            elif agent.state in ("failed", "stopped") or layout.stalled(agent):
                bad += 1
            elif agent.state not in layout.SETTLED:
                running += 1
            continue
        interactive += 1
        if agent is not None and agent.live:
            live += 1
    # the last slot is a remainder, so the four always sum to the group's count
    return Tally(interactive, live, background, running, blocked, bad,
                 background - running - blocked - bad)


@dataclass
class UIState:
    query: str = ""
    sel: int = 0
    top: int = 0
    count: int = 0             # display rows in view, group headers included
    shown: int = 0             # session rows in view
    total: int = 0             # session rows before filtering
    skip: frozenset[int] = frozenset()   # view positions holding a group header
    tally: Tally = Tally()     # what the session rows in view are
    page: int = 10             # visible list rows
    preview: bool = False
    preview_off: int = 0
    preview_len: int = 0
    preview_page: int = 10
    sort: Sort = Sort.RECENT
    group: Group = Group.NONE
    all_projects: bool = False
    branch: str = ""           # current branch of the scope directory, '' when not a repo

    def select(self, idx: int, direction: int = 1) -> None:
        if self.count <= 0:
            self.sel = self.top = 0
            return
        self.sel = self._landing(max(0, min(idx, self.count - 1)), direction)
        page = max(1, self.page)
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + page:
            self.top = self.sel - page + 1
        self.top = max(0, min(self.top, max(0, self.count - page)))
        # the headers directly above the selection are its label: keep them on
        # screen, but never far enough up to push the selection off it
        head = self.sel
        while head - 1 in self.skip:
            head -= 1
        if head < self.sel:
            self.top = max(self.sel - page + 1, min(self.top, head))

    def move(self, delta: int) -> None:
        self.select(self.sel + delta, 1 if delta >= 0 else -1)

    def _landing(self, idx: int, direction: int) -> int:
        """The nearest selectable row: on past the headers, turning round at the end."""
        if idx not in self.skip:
            return idx
        for step in (direction, -direction):
            pos = idx + step
            while 0 <= pos < self.count:
                if pos not in self.skip:
                    return pos
                pos += step
        return idx

    def scroll_preview(self, delta: int) -> None:
        limit = max(0, self.preview_len - max(1, self.preview_page))
        self.preview_off = max(0, min(self.preview_off + delta, limit))

    def toggle_preview(self) -> None:
        self.preview = not self.preview
        self.preview_off = 0


def handle_key(state: UIState, key) -> Effect:
    """Apply one key. State moves here; anything with a side effect is returned."""
    if key is None:
        return Effect(NONE)
    if key == curses.KEY_RESIZE:
        return Effect(RESIZE)

    if key in (curses.KEY_UP, "\x0b"):
        state.move(-1)
        return Effect(MOVE)
    if key in (curses.KEY_DOWN, "\n"):
        state.move(1)
        return Effect(MOVE)
    if key == curses.KEY_HOME:
        state.select(0, 1)
        return Effect(MOVE)
    if key == curses.KEY_END:
        state.select(state.count - 1, -1)
        return Effect(MOVE)
    if key in (curses.KEY_SR, curses.KEY_SPREVIOUS):
        state.scroll_preview(-1)
        return Effect(PREVIEW)
    if key in (curses.KEY_SF, curses.KEY_SNEXT):
        state.scroll_preview(1)
        return Effect(PREVIEW)
    if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
        step = -1 if key == curses.KEY_PPAGE else 1
        # a scrolled preview keeps the paging keys until it is back at the top
        if state.preview and state.preview_off > 0:
            state.scroll_preview(step * max(1, state.preview_page))
            return Effect(PREVIEW)
        state.move(step * max(1, state.page))
        return Effect(MOVE)

    if key == "\x1b":
        if state.preview:
            state.toggle_preview()
            return Effect(PREVIEW)
        return Effect(QUIT)
    if key == "\x03":
        return Effect(QUIT)
    if key in ("\r", curses.KEY_ENTER):
        return Effect(RESUME)
    if key == "\x06":
        return Effect(FORK)
    if key == "\x0e":
        return Effect(NEW)
    if key == "\x04":
        return Effect(DISPATCH)
    if key == "\x12":
        return Effect(RENAME)
    if key == "\x18":
        return Effect(DELETE)
    if key == "\x14":
        return Effect(PRUNE)
    if key == "\x0f":
        return Effect(LINKS)
    if key == "\x07":
        return Effect(CYCLE_AGENT)
    if key == "\x02":
        if not state.branch:
            return Effect(NONE)
        return Effect(TOGGLE_TOKEN, BRANCH_TOKEN + state.branch)
    if key == "\x10":
        state.toggle_preview()
        return Effect(PREVIEW)
    if key == "\x01":
        return Effect(TOGGLE_SCOPE)
    if key == "\x13":
        return Effect(CYCLE_SORT)
    if key == "\x05":
        return Effect(CYCLE_GROUP)
    if key == "\x0c":
        return Effect(RELOAD)

    if key == " ":
        # space only toggles the preview when there is no query to extend
        if state.query and not state.query.endswith(" "):
            state.query += " "
            return Effect(QUERY)
        state.toggle_preview()
        return Effect(PREVIEW)

    text, handled = widgets.edit_line(state.query, key)
    if handled and text != state.query:
        state.query = text
        return Effect(QUERY)
    return Effect(NONE)


# --------------------------------------------------------------------------- #
# chrome
# --------------------------------------------------------------------------- #


def prompt_row(query: str) -> Row:
    return [(PROMPT, Style.PROMPT), (query, Style.QUERY)]


GAP = "   "


def _scope_text(target: str, all_projects: bool, snap: Snapshot | None) -> str:
    if all_projects:
        encoded = {p.encoded for p in snap.projects.values()} if snap is not None else set()
        return f"all projects ({len(encoded)})"
    home = paths.HOME
    if home and (target == home or target.startswith(home + "/")):
        return "~" + target[len(home):]
    return target


def _bracket(*slots: tuple[int, str]) -> Row:
    """A group's breakdown, subordinate to its own count; empty slots go unsaid."""
    row: Row = []
    for n, state in slots:
        if not n:
            continue
        row.append(("/" if row else " [", Style.COUNTER))
        row.append((str(n), STATE_STYLE[state]))
    if row:
        row.append(("]", Style.COUNTER))
    return row


def _interactive_group(tally: Tally) -> Row:
    """The sessions that are not background agents, and how many are live."""
    if not tally.interactive:
        return []
    return [(f"{tally.interactive} interactive", Style.COUNTER)] + _bracket((tally.live, "live"))


def _background_group(tally: Tally) -> Row:
    """The background agents, split into slots that add up to their count."""
    if not tally.background:
        return []
    return [(f"{tally.background} background", Style.COUNTER)] + _bracket(
        (tally.running, "live"), (tally.blocked, "blocked"),
        (tally.bad, "failed"), (tally.done, "done"))


def status_row(shown: int, total: int, tally: Tally, target: str, all_projects: bool,
               snap: Snapshot | None = None, width: int = 0) -> Row:
    """Where we are looking, how much is shown, and what is running.

    The groups describe the rows the query kept, so they sum to ``shown``; the
    counter's total stays global. ``width`` of 0 means no limit; otherwise whole
    segments give way until the line fits. The counter and the error stay
    whatever happens.
    """
    scope = _scope_text(target, all_projects, snap)
    labelled: Row = [(f"{shown}/{total} sessions", Style.COUNTER)]
    digits: Row = [(f"{shown}/{total}", Style.COUNTER)]
    interactive = _interactive_group(tally)
    background = _background_group(tally)
    scanning: Row = []
    error: Row = []
    if snap is not None and snap.scanning:
        scanning = [(f"scanning {snap.scanned}/{snap.total}", Style.COUNTER)]
    if snap is not None and snap.error:
        error = [(snap.error, Style.MARKER)]

    def join(counter: Row, scope_text: str, *counts: Row, scan: Row = ()) -> Row:
        row: Row = [("  ", Style.NORMAL)]
        if scope_text:
            row += [(scope_text, Style.HEADER), (GAP, Style.NORMAL)]
        chain: Row = []
        for segment in (counter, *counts):
            if not segment:
                continue
            if chain:
                chain.append((" · ", Style.COUNTER))
            chain += segment
        row += chain
        for tail in (scan, error):
            if tail:
                row += [(GAP, Style.NORMAL), *tail]
        return row

    # the counter's noun is the last thing to go before the scope shrinks: it
    # must never be what pushes the digits themselves off the line
    for counter, counts, scan in ((labelled, (interactive, background), scanning),
                                  (labelled, (interactive,), scanning),
                                  (labelled, (), scanning), (labelled, (), []),
                                  (digits, (), [])):
        row = join(counter, scope, *counts, scan=scan)
        if not width or layout.dw(plain(row)) <= width:
            return row
    # the scope is the last to give way, and it shrinks before it goes
    budget = width - layout.dw(plain(join(digits, ""))) - len(GAP)
    return join(digits, layout.dtrunc(scope, budget))


HINTS_MAIN = [
    ("Enter", "resume/attach"),
    ("Ctrl+N", "new session"),
    ("Ctrl+D", "background agent"),
    ("Ctrl+F", "fork"),
    ("Ctrl+R", "rename"),
    ("Ctrl+X", "delete"),
    ("Ctrl+T", "prune stubs"),
    ("Ctrl+O", "open links"),
]


def _hint_row(hints: list[tuple[str, str]], width: int) -> Row:
    row: Row = []
    used = 0
    for key, label in hints:
        gap = 3 if row else 0
        need = gap + layout.dw(key) + 1 + layout.dw(label)
        if used + need > width:
            break
        if gap:
            row.append((" " * gap, Style.NORMAL))
        row.append((key, Style.FOOTER))
        row.append((" " + label, Style.DIM))
        used += need
    return row


def agent_token(query: str) -> str:
    """Which of the agent filter tokens the query carries, if any."""
    fields = [f for f in query.split(" ") if f]
    for token in AGENT_CYCLE:
        if token in fields:
            return token
    return ""


def footer_lines(width: int, branch: str = "", sort: Sort = Sort.RECENT,
                 all_projects: bool = False, group: Group = Group.NONE,
                 agent: str = "") -> list[Row]:
    """Two lines of legend, dropping the least important hints when narrow."""
    second: list[tuple[str, str]] = [
        # the token in force, spelt as the query line spells it; both tokens when
        # no filter is on, so the key shows what it reaches
        ("Ctrl+G", agent or "bg:/live:"),
        ("Ctrl+A", "this project" if all_projects else "all projects"),
        ("Ctrl+S", f"sort={sort.label}"),
        ("Ctrl+E", f"group={group.label}"),
    ]
    if branch:
        # last of the mode keys, and capped at the branch budget so a long name
        # never costs the others their place on the line
        second.append(("Ctrl+B", f"branch={layout.dtrunc(branch, layout.BR_MAX)}"))
    second += [
        ("Space/Ctrl+P", "preview"),
        ("Esc", "close preview/quit"),
        ("Ctrl+L", "reload"),
    ]
    return [_hint_row(HINTS_MAIN, width), _hint_row(second, width)]


def preview_header(sid: str, off: int, total: int) -> tuple[str, str]:
    label = f"  {sid[:8]}  "
    indicator = f"{min(off + 1, max(total, 1))}/{max(total, 1)}"
    return label, indicator


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #


class App:
    def __init__(self, screen: Screen, scope: store.Scope, sort: Sort = Sort.RECENT,
                 group: Group = Group.NONE, use_cache: bool = True) -> None:
        self.screen = screen
        self.target = scope.cwd
        self.use_cache = use_cache
        self.state = UIState(sort=sort, group=group, all_projects=scope.all_projects,
                             branch=gitinfo.current_branch(scope.cwd) or "")
        self.snap = Snapshot()
        self.rows: list[Row] = []
        self.sids: list[str] = []
        self.labels: list[str] = []
        self.hays: list[str] = []
        self.view: list[int] = []
        self.anchor = ""
        self.running = True
        self.last_poll = 0.0
        self._preview_key: tuple = ()
        self._preview_rows: list[Row] = []
        self._row_width = 0
        self.loader = self._new_loader()

    # -- wiring ------------------------------------------------------------- #

    def _scope(self) -> store.Scope:
        return (store.Scope.everything(self.target) if self.state.all_projects
                else store.Scope.project(self.target))

    def _new_loader(self) -> store.Loader:
        loader = store.Loader(self._scope(), sort=self.state.sort, use_cache=self.use_cache)
        loader.start()
        return loader

    def close(self) -> None:
        self.loader.close()

    # -- data --------------------------------------------------------------- #

    def sync(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self.last_poll >= POLL_SECS:
            self.last_poll = now
            if force or self.loader.dirty():
                self.loader.request_reload()
        snap = self.loader.snapshot()
        if snap is not self.snap or force:
            self.snap = snap
            self.rebuild()

    def list_width(self) -> int:
        width = self.screen.width
        pane = int(width * PREVIEW_FRACTION) if self.state.preview else 0
        # two columns go to the selection gutter
        return max(1, width - pane - 2)

    def rebuild(self) -> None:
        self._row_width = self.list_width()
        listing = layout.build_list(self.snap, self.snap.roots, self.snap.children,
                                    self._row_width, self.state.all_projects, self.state.group)
        self.rows, self.sids, self.labels = listing.rows, listing.sids, listing.labels
        self.hays = []
        self.state.total = sum(1 for sid in self.sids if sid)
        for i, (sid, row) in enumerate(zip(self.sids, self.rows)):
            # a header has no haystack of its own; it lives or dies with its block
            if not sid:
                self.hays.append("")
                continue
            session = self.snap.sessions.get(sid)
            project = self.snap.project_of(sid)
            # the block label and the project name are searchable in every mode,
            # whichever of them the row and its header happen to show
            self.hays.append(" ".join((plain(row),
                                       self.labels[i],
                                       project.name if project else "",
                                       session.hay if session else "")))
        self._preview_key = ()
        self.refilter()

    def refilter(self) -> None:
        query = fuzzy.parse(self.state.query)
        view: list[int] = []
        skip: set[int] = set()
        pending = -1                       # a header waiting for a row to name
        head = -1                          # the header the rows counted so far sit under
        kept = 0
        for i, sid in enumerate(self.sids):
            if not sid:
                if head >= 0:
                    self.rows[head] = layout.group_header(self.labels[head], kept,
                                                          self._row_width)
                pending, head, kept = i, -1, 0
                continue
            if not fuzzy.matches(query, self.hays[i]):
                continue
            if pending >= 0:
                skip.add(len(view))
                view.append(pending)
                head, kept, pending = pending, 0, -1
            view.append(i)
            kept += 1
        if head >= 0:
            # a header counts what the query left of its block, not the block
            self.rows[head] = layout.group_header(self.labels[head], kept, self._row_width)
        self.view = view
        self.state.skip = frozenset(skip)
        self.state.count = len(view)
        self.state.shown = len(view) - len(skip)
        # a group header is not a session, so the tally is of the rows kept
        self.state.tally = _tally([self.sids[i] for i in view if self.sids[i]],
                                  self.snap.agents)
        self.restore_selection()
        self._preview_key = ()

    def restore_selection(self) -> None:
        if self.anchor:
            for pos, idx in enumerate(self.view):
                if self.sids[idx] == self.anchor:
                    self.state.select(pos)
                    return
        self.state.select(self.state.sel)

    def current(self) -> Session | None:
        if not self.view:
            return None
        sid = self.sids[self.view[min(self.state.sel, len(self.view) - 1)]]
        if not sid:
            return None
        self.anchor = sid
        return self.snap.sessions.get(sid)

    # -- preview ------------------------------------------------------------ #

    def preview_rows(self, width: int) -> list[Row]:
        session = self.current()
        if session is None:
            return []
        key = (session.sid, self.state.query, width)
        if key != self._preview_key:
            rows = preview.render(session.sid, self.snap, self.state.query)
            self._preview_rows = preview.wrap(rows, width)
            self._preview_key = key
        return self._preview_rows

    # -- drawing ------------------------------------------------------------ #

    def draw(self) -> None:
        scr = self.screen
        win = scr.stdscr
        height, width = scr.size
        scr.erase()
        if height < 7 or width < 30:
            scr.draw_text(win, 0, 0, "terminal too small", Style.MARKER, width)
            scr.refresh()
            return

        list_h = height - 5
        self.state.page = list_h
        pane = int(width * PREVIEW_FRACTION) if self.state.preview else 0
        border_x = width - pane
        list_w = border_x

        scr.draw_row(win, 0, 0, prompt_row(self.state.query), width)
        scr.draw_row(win, 1, 0, status_row(self.state.shown, self.state.total,
                                           self.state.tally, self.target,
                                           self.state.all_projects, self.snap, width), width)

        if not self.view:
            note = "scanning…" if self.snap.scanning else "no sessions"
            scr.draw_text(win, 2, 2, note, Style.DIM, list_w - 2)
        for i in range(list_h):
            pos = self.state.top + i
            if pos >= len(self.view):
                break
            row = self.rows[self.view[pos]]
            selected = pos == self.state.sel
            bg = SELECTED_BG if selected else -1
            y = 2 + i
            if selected:
                scr.fill(win, y, 0, list_w, bg)
                scr.draw_text(win, y, 0, GUTTER, Style.GUTTER, 1, bg)
            scr.draw_row(win, y, 2, row, list_w - 2, bg, bold=selected)

        if self.state.preview:
            self.draw_preview(win, 2, border_x, list_h, width)

        scr.draw_text(win, height - 3, 0, "─" * width, Style.BORDER, width)
        footer = footer_lines(width, self.state.branch, self.state.sort,
                              self.state.all_projects, self.state.group,
                              agent_token(self.state.query))
        for i, row in enumerate(footer):
            scr.draw_row(win, height - 2 + i, 0, row, width)

        scr.place_cursor(0, layout.dw(PROMPT) + layout.dw(self.state.query))
        scr.refresh()

    def draw_preview(self, win, top: int, border_x: int, list_h: int, width: int) -> None:
        scr = self.screen
        content_x = border_x + 2
        content_w = max(1, width - content_x)
        rows = self.preview_rows(content_w)
        self.state.preview_len = len(rows)
        self.state.preview_page = max(1, list_h - 1)
        session = self.current()
        for i in range(list_h):
            scr.draw_text(win, top + i, border_x, "│", Style.BORDER, 1)
        label, indicator = preview_header(session.sid if session else "", self.state.preview_off, len(rows))
        scr.draw_text(win, top, border_x, label, Style.BORDER, width - border_x)
        scr.draw_text(win, top, max(border_x + 1, width - layout.dw(indicator)), indicator,
                      Style.BORDER, layout.dw(indicator))
        for i in range(1, list_h):
            idx = self.state.preview_off + i - 1
            if idx >= len(rows):
                break
            scr.draw_row(win, top + i, content_x, rows[idx], content_w)

    # -- actions ------------------------------------------------------------ #

    def open_session(self, fork: bool) -> None:
        session = self.current()
        if session is None:
            return
        live = actions.is_live(session, self.snap)
        # a fork starts a session of its own, so only a resume can land in a
        # terminal that already holds this one
        if (not fork and actions.is_live_interactive(session, self.snap)
                and not self.confirm_second_terminal(session)):
            return
        argv = actions.fork_command(session) if fork else actions.resume_command(session, live)
        self.screen.run(argv, actions.work_dir(session, self.snap, self.target))
        self.sync(force=True)

    def confirm_second_terminal(self, session: Session) -> bool:
        """Resuming a session another terminal holds: the default is to leave it be."""
        agent = self.snap.agents.get(session.sid)
        lines = [f"Session: {session.title}"]
        if agent is not None and agent.status:
            lines.append(f"Status:  {agent.status}")
        lines += ["", "This session is already open in another terminal.",
                  "Resuming it here puts two terminals on one session.",
                  "", "Resume anyway?"]
        return widgets.confirm(self.screen, "Session already open", lines)

    def new_session(self) -> None:
        """Start a session, named if asked for, then come back to the list."""
        name = widgets.text_input(self.screen, "New session", "name (optional): ")
        if name is None:
            return
        cwd = actions.new_dir(self.current(), self.snap, self.target, self.state.all_projects)
        self.screen.run(actions.new_command(name), cwd)
        self.sync(force=True)

    def dispatch_agent(self) -> None:
        """Queue a background agent. It runs without the terminal, so the list stays up."""
        prompt = widgets.text_area(
            self.screen, "Background agent", "prompt: ", multiline=True, rows=widgets.AREA_ROWS
        )
        if not prompt or not prompt.strip():
            return
        cwd = actions.new_dir(self.current(), self.snap, self.target, self.state.all_projects)
        step = actions.dispatch(prompt.strip(), cwd)
        if not step.ok:
            widgets.message(self.screen, "Dispatch failed", step.lines())
        self.sync(force=True)

    def do_rename(self) -> None:
        session = self.current()
        if session is None:
            return
        current = actions.current_title(session)
        name = widgets.text_input(self.screen, f"Rename {session.sid[:8]}", "name: ", current)
        if name is None:
            return
        error = actions.rename(session, name)
        if error:
            widgets.message(self.screen, "Rename failed", [error])
        self.sync(force=True)

    def do_delete(self) -> None:
        session = self.current()
        if session is None:
            return
        plan = actions.delete(session, self.snap)
        if not widgets.confirm(self.screen, "Delete session", plan.lines()):
            return
        report = actions.perform_delete(plan)
        widgets.message(self.screen, "Delete", report.lines())
        self.anchor = ""
        self.sync(force=True)

    def do_prune(self) -> None:
        rows = actions.prune_stubs(self.snap)
        if not rows:
            widgets.message(self.screen, "Prune stubs", ["no stub sessions here"])
            return
        lines = ["Stub sessions (transcript with no turns, not resumable):", ""]
        lines += [r.line() for r in rows]
        prunable = [r for r in rows if not r.skipped]
        lines += ["", f"Prune {len(prunable)} of {len(rows)} stub session(s)?"]
        if not widgets.confirm(self.screen, "Prune stubs", lines):
            return
        report = actions.perform_prune(rows)
        widgets.message(self.screen, "Prune stubs", report.lines())
        self.anchor = ""
        self.sync(force=True)

    def do_links(self) -> None:
        session = self.current()
        if session is None:
            return
        found = actions.links(session, self.snap)
        if not found:
            widgets.message(self.screen, "Links", ["no links"])
            return
        if len(found) == 1:
            self.open_url(found[0].url)
            return
        items = [f"{link.label:<32} {link.url}" for link in found]
        choice = widgets.picker(self.screen, "Open link", items)
        if choice is None:
            return
        targets = found if choice == widgets.ALL else [found[choice]]
        for link in targets:
            self.open_url(link.url)

    def open_url(self, url: str) -> None:
        error = actions.open_link(url)
        if error:
            widgets.message(self.screen, "Open failed", [error])

    def set_scope(self, all_projects: bool) -> None:
        self.state.all_projects = all_projects
        self.loader.set_scope(self._scope())
        self.snap = Snapshot()
        self.sync(force=True)

    def cycle_sort(self) -> None:
        self.state.sort = self.state.sort.next()
        self.loader.set_sort(self.state.sort)
        self.sync(force=True)

    def cycle_group(self) -> None:
        """Purely a relayout: the data on screen is already the data it needs."""
        self.state.group = self.state.group.next()
        self.rebuild()

    # -- loop --------------------------------------------------------------- #

    def apply(self, effect: Effect) -> None:
        kind = effect.kind
        if kind == QUIT:
            self.running = False
        elif kind == QUERY:
            self.refilter()
        elif kind == TOGGLE_TOKEN:
            self.state.query = fuzzy.toggle_token(self.state.query, effect.arg)
            self.refilter()
        elif kind == CYCLE_AGENT:
            self.state.query = fuzzy.cycle_token(self.state.query, AGENT_CYCLE)
            self.refilter()
        elif kind == RESIZE:
            self.rebuild()
        elif kind == RELOAD:
            self.sync(force=True)
        elif kind == RESUME:
            self.open_session(fork=False)
        elif kind == FORK:
            self.open_session(fork=True)
        elif kind == NEW:
            self.new_session()
        elif kind == DISPATCH:
            self.dispatch_agent()
        elif kind == RENAME:
            self.do_rename()
        elif kind == DELETE:
            self.do_delete()
        elif kind == PRUNE:
            self.do_prune()
        elif kind == LINKS:
            self.do_links()
        elif kind == TOGGLE_SCOPE:
            self.set_scope(not self.state.all_projects)
        elif kind == CYCLE_SORT:
            self.cycle_sort()
        elif kind == CYCLE_GROUP:
            self.cycle_group()
        elif kind == MOVE:
            self.current()

    def run(self) -> int:
        try:
            self.sync(force=True)
            while self.running:
                width = self.list_width()
                if width != self._row_width:
                    self.rebuild()
                self.draw()
                key = self.screen.get_key(TICK_MS)
                if key is None:
                    self.sync()
                    continue
                self.apply(handle_key(self.state, key))
        finally:
            self.close()
        return 0


def run(scope: store.Scope, sort: Sort = Sort.RECENT, group: Group = Group.NONE,
        use_cache: bool = True) -> int:
    """Entry point for cli.py: take the terminal, show the list, give it back."""
    return launch(lambda scr: App(scr, scope, sort, group, use_cache).run())
