"""Import-light ``pytest11`` plugin providing ``vt_*`` fixtures."""

from __future__ import annotations

import typing as t

import pytest

if t.TYPE_CHECKING:
    import collections.abc

    from libvt.parser import Parser
    from libvt.terminal import Effects, Terminal


@pytest.fixture
def vt_terminal() -> Terminal:
    """Return a fresh ``24x80`` :class:`~libvt.terminal.Terminal` instance."""
    from libvt.terminal import Terminal

    return Terminal(rows=24, cols=80)


@pytest.fixture
def vt_factory() -> collections.abc.Callable[..., Terminal]:
    """Return a factory creating :class:`~libvt.terminal.Terminal` instances."""
    from libvt.terminal import Terminal

    def _make(
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
        effects: Effects | None = None,
    ) -> Terminal:
        return Terminal(
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            effects=effects,
        )

    return _make


@pytest.fixture
def vt_parser() -> Parser:
    """Return a fresh streaming :class:`~libvt.parser.Parser`."""
    from libvt.parser import Parser

    return Parser()


__all__ = [
    "vt_factory",
    "vt_parser",
    "vt_terminal",
]
