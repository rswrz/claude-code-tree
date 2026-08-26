"""Curses interaction layer. Nothing outside this package imports curses."""

from __future__ import annotations

from .app import run

__all__ = ["run"]
