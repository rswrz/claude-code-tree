"""The list screen: state, key handling, drawing and the main loop.

Key handling is a pure function over :class:`UIState` returning an
:class:`Effect`, so the interaction table is testable without a terminal.
Side effects all live in :class:`App`.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass

from cct import actions, fuzzy, gitinfo, layout, preview, store, tree
from cct.model import Session, Snapshot, Sort
from cct.theme import SELECTED_BG, Row, Style, plain

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
BG_TOKEN = "bg:"


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
RENAME = "rename"
DELETE = "delete"
PRUNE = "prune"
LINKS = "links"
TOGGLE_TOKEN = "toggle_token"
TOGGLE_SCOPE = "toggle_scope"
CYCLE_SORT = "cycle_sort"


@dataclass(frozen=True)
class Effect:
    kind: str = NONE
    arg: str = ""


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


@dataclass
class UIState:
    query: str = ""
    sel: int = 0
    top: int = 0
    count: int = 0             # rows after filtering
    total: int = 0             # rows before filtering
    page: int = 10             # visible list rows
    preview: bool = False
    preview_off: int = 0
    preview_len: int = 0
    preview_page: int = 10
    sort: Sort = Sort.RECENT
    all_projects: bool = False
    branch: str = ""           # current branch of the scope directory, '' when not a repo

    def select(self, idx: int) -> None:
        if self.count <= 0:
            self.sel = self.top = 0
            return
        self.sel = max(0, min(idx, self.count - 1))
        page = max(1, self.page)
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + page:
            self.top = self.sel - page + 1
        self.top = max(0, min(self.top, max(0, self.count - page)))

    def move(self, delta: int) -> None:
        self.select(self.sel + delta)

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
        state.select(0)
        return Effect(MOVE)
    if key == curses.KEY_END:
        state.select(state.count - 1)
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
    if key == "\x12":
        return Effect(RENAME)
    if key == "\x18":
        return Effect(DELETE)
    if key == "\x14":
        return Effect(PRUNE)
    if key == "\x0f":
        return Effect(LINKS)
    if key == "\x07":
        return Effect(TOGGLE_TOKEN, BG_TOKEN)
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


def counter_row(shown: int, total: int, snap: Snapshot | None = None) -> Row:
    row: Row = [(f"  {shown}/{total}", Style.COUNTER)]
    if snap is not None and snap.scanning:
        row.append((f"   scanning {snap.scanned}/{snap.total}", Style.COUNTER))
    if snap is not None and snap.error:
        row.append((f"   {snap.error}", Style.MARKER))
    return row


def header_row(target: str, all_projects: bool, n_projects: int) -> Row:
    if all_projects:
        return [(f"  all projects ({n_projects})", Style.HEADER)]
    return [(f"  {target}", Style.HEADER)]


HINTS_MAIN = [
    ("Enter", "resume/attach"),
    ("Ctrl+N", "new session"),
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


def footer_lines(width: int, branch: str = "", sort: Sort = Sort.RECENT,
                 all_projects: bool = False) -> list[Row]:
    """Two lines of legend, dropping the least important hints when narrow."""
    second: list[tuple[str, str]] = [("Ctrl+G", "background")]
    if branch:
        second.append(("Ctrl+B", f"branch={branch}"))
    second += [
        ("Ctrl+A", "this project" if all_projects else "all projects"),
        ("Ctrl+S", f"sort={sort.label}"),
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
                 use_cache: bool = True) -> None:
        self.screen = screen
        self.target = scope.cwd
        self.use_cache = use_cache
        self.state = UIState(sort=sort, all_projects=scope.all_projects,
                             branch=gitinfo.current_branch(scope.cwd) or "")
        self.snap = Snapshot()
        self.rows: list[Row] = []
        self.sids: list[str] = []
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
        rows = tree.walk(self.snap.roots, self.snap.children)
        self._row_width = self.list_width()
        self.sids = [r.sid for r in rows]
        self.rows = layout.build_rows(self.snap, rows, self._row_width, self.state.all_projects)
        self.hays = [
            plain(row) + " " + (self.snap.sessions[sid].hay if sid in self.snap.sessions else "")
            for sid, row in zip(self.sids, self.rows)
        ]
        self._preview_key = ()
        self.refilter()

    def refilter(self) -> None:
        query = fuzzy.parse(self.state.query)
        self.view = [i for i, hay in enumerate(self.hays) if fuzzy.matches(query, hay)]
        self.state.count = len(self.view)
        self.state.total = len(self.rows)
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
        if height < 8 or width < 30:
            scr.draw_text(win, 0, 0, "terminal too small", Style.MARKER, width)
            scr.refresh()
            return

        list_h = height - 6
        self.state.page = list_h
        pane = int(width * PREVIEW_FRACTION) if self.state.preview else 0
        border_x = width - pane
        list_w = border_x

        scr.draw_row(win, 0, 0, prompt_row(self.state.query), width)
        scr.draw_row(win, 1, 0, counter_row(self.state.count, self.state.total, self.snap), width)
        scr.draw_row(win, 2, 0, header_row(self.target, self.state.all_projects,
                                           len(set(p.encoded for p in self.snap.projects.values()))), width)

        if not self.view:
            note = "scanning…" if self.snap.scanning else "no sessions"
            scr.draw_text(win, 3, 2, note, Style.DIM, list_w - 2)
        for i in range(list_h):
            pos = self.state.top + i
            if pos >= len(self.view):
                break
            row = self.rows[self.view[pos]]
            selected = pos == self.state.sel
            bg = SELECTED_BG if selected else -1
            y = 3 + i
            if selected:
                scr.fill(win, y, 0, list_w, bg)
                scr.draw_text(win, y, 0, GUTTER, Style.GUTTER, 1, bg)
            scr.draw_row(win, y, 2, row, list_w - 2, bg, bold=selected)

        if self.state.preview:
            self.draw_preview(win, 3, border_x, list_h, width)

        scr.draw_text(win, height - 3, 0, "─" * width, Style.BORDER, width)
        for i, row in enumerate(footer_lines(width, self.state.branch, self.state.sort,
                                             self.state.all_projects)):
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
        argv = actions.fork_command(session) if fork else actions.resume_command(session, live)
        self.screen.run(argv, actions.work_dir(session, self.snap, self.target))
        self.sync(force=True)

    def new_session(self) -> None:
        """Start a session in the directory the list is pointed at."""
        cwd = actions.new_dir(self.current(), self.snap, self.target, self.state.all_projects)
        self.screen.run(actions.new_command(), cwd)
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


def run(scope: store.Scope, sort: Sort = Sort.RECENT, use_cache: bool = True) -> int:
    """Entry point for cli.py: take the terminal, show the list, give it back."""
    return launch(lambda scr: App(scr, scope, sort, use_cache).run())
