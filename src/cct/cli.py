"""Argument parsing, plus ``--print`` and ``--preview``.

Imports are deferred into the function that needs them, so ``curses`` is only
ever loaded for the interactive path.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import TYPE_CHECKING

from cct import __version__
from cct.model import Sort
from cct.theme import PALETTE, Style

if TYPE_CHECKING:
    from cct.theme import Row


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-code-tree",
        description="Browse and manage Claude Code sessions as a tree.",
    )
    parser.add_argument(
        "dir",
        nargs="?",
        default=None,
        metavar="DIR",
        help="project directory to browse (default: current directory)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="start in all-projects scope",
    )
    parser.add_argument(
        "--sort",
        choices=[s.value for s in Sort],
        default=Sort.RECENT.value,
        help="initial sort order (default: recent)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore the on-disk transcript cache",
    )
    parser.add_argument(
        "--print",
        dest="print_",
        action="store_true",
        help="render the tree to stdout with ANSI colours and exit",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        metavar="N",
        help="terminal width to render for, with --print / --preview "
        "(default: the real terminal width)",
    )
    parser.add_argument(
        "--preview",
        metavar="SID",
        default=None,
        help="render one session's preview pane to stdout and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _resolve_target(dir_arg: str | None) -> str:
    from cct.paths import resolve_dir

    return resolve_dir(dir_arg or os.getcwd())


def _make_scope(*, abs_dir: str, all_projects: bool):
    """Build the ``cct.store.Scope`` the rest of ``cli.py`` loads data through."""
    from cct.store import Scope

    return Scope.everything(abs_dir) if all_projects else Scope.project(abs_dir)


def _ansi_for(style: Style) -> str:
    ink = PALETTE.get(style)
    if ink is None:
        return ""
    parts = []
    if ink.bold:
        parts.append("1")
    if ink.dim:
        parts.append("2")
    if ink.reverse:
        parts.append("7")
    if ink.fg >= 0:
        parts.append(f"38;5;{ink.fg}")
    return f"\x1b[{';'.join(parts)}m" if parts else ""


def _ansi_line(row: "Row") -> str:
    out = []
    for text, style in row:
        code = _ansi_for(style)
        out.append(f"{code}{text}\x1b[0m" if code else text)
    return "".join(out)


GUTTER_COLS = 2
MIN_PRINT_WIDTH = 38


def _terminal_width(explicit: int | None) -> int:
    if explicit and explicit > 0:
        return explicit
    return shutil.get_terminal_size(fallback=(200, 24)).columns


def _run_print(args: argparse.Namespace) -> int:
    from cct.layout import build_rows
    from cct.store import scan_once
    from cct.tree import walk

    abs_dir = _resolve_target(args.dir)
    sort = Sort(args.sort)
    scope = _make_scope(abs_dir=abs_dir, all_projects=args.all)
    snap = scan_once(scope, sort, use_cache=not args.no_cache)

    if snap.error:
        print(f"claude-code-tree: {snap.error}", file=sys.stderr)
        return 1

    tree_rows = walk(snap.roots, snap.children)
    if not tree_rows:
        where = "all projects" if args.all else abs_dir
        print(f"claude-code-tree: no Claude sessions found for {where}", file=sys.stderr)
        return 1

    # the TUI reserves two columns for the selection gutter; --print matches it
    # so that a given --width renders exactly what that terminal width would show
    width = max(MIN_PRINT_WIDTH, _terminal_width(args.width) - GUTTER_COLS)
    rows = build_rows(snap, tree_rows, width, show_project=args.all)
    for row in rows:
        print(_ansi_line(row))
    return 0


def _run_preview(args: argparse.Namespace) -> int:
    from cct.preview import render, wrap
    from cct.store import scan_once

    # A session id is looked up globally: it may live under any project directory.
    scope = _make_scope(abs_dir="", all_projects=True)
    snap = scan_once(scope, Sort.RECENT, use_cache=not args.no_cache)

    width = _terminal_width(args.width)
    rows = wrap(render(args.preview, snap), width)
    if not rows:
        print(f"claude-code-tree: no transcript or job state for {args.preview}", file=sys.stderr)
        return 1
    for row in rows:
        print(_ansi_line(row))
    return 0


def _run_tui(args: argparse.Namespace) -> int:
    from cct.ui.app import run

    abs_dir = _resolve_target(args.dir)
    sort = Sort(args.sort)
    scope = _make_scope(abs_dir=abs_dir, all_projects=args.all)
    return run(scope, sort=sort, use_cache=not args.no_cache) or 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.preview:
            return _run_preview(args)
        if args.print_:
            return _run_print(args)
        return _run_tui(args)
    except NotADirectoryError as exc:
        print(f"claude-code-tree: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
