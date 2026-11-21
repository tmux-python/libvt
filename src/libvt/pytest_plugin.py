"""Import-light ``pytest11`` plugin providing ``vt_*`` fixtures."""

from __future__ import annotations

import sys
import typing as t

import pytest

if t.TYPE_CHECKING:
    import collections.abc
    import os

    from libvt.aio import PtySession
    from libvt.config import Limits, Policy, Profile
    from libvt.events import Event
    from libvt.parser import Parser
    from libvt.terminal import FlightRecorder, Terminal


class TerminalFactory(t.Protocol):
    """Typed callable protocol returned by the :func:`vt_factory` fixture."""

    def __call__(
        self,
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
        profile: Profile = ...,
        policy: Policy = ...,
        limits: Limits = ...,
        on_event: collections.abc.Callable[[Event], None] | None = None,
        record_events: bool = False,
        recorder: FlightRecorder | None = None,
    ) -> Terminal:
        """Create a configured :class:`~libvt.terminal.Terminal` instance."""


class PtyFactory(t.Protocol):
    """Typed async context-manager factory returned by :func:`vt_pty`."""

    def __call__(
        self,
        argv: collections.abc.Sequence[str],
        *,
        rows: int = 24,
        cols: int = 80,
        env: collections.abc.Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> contextlib.AbstractAsyncContextManager[PtySession]:
        """Open a POSIX PTY subprocess session."""


if t.TYPE_CHECKING:
    import contextlib


@pytest.fixture
def vt_recorder() -> FlightRecorder:
    """Return a fresh 4 KiB :class:`~libvt.terminal.FlightRecorder`."""
    from libvt.terminal import FlightRecorder

    return FlightRecorder()


@pytest.fixture
def vt_terminal(vt_recorder: FlightRecorder) -> Terminal:
    """Return a fresh ``24x80`` :class:`~libvt.terminal.Terminal` with recorder."""
    from libvt.terminal import Terminal

    return Terminal(rows=24, cols=80, recorder=vt_recorder)


@pytest.fixture
def vt_factory() -> TerminalFactory:
    """Return a typed factory creating :class:`~libvt.terminal.Terminal` instances."""
    from libvt.config import DEFAULT_LIMITS, DEFAULT_POLICY, DEFAULT_PROFILE
    from libvt.terminal import Terminal

    def _make(
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
        profile: Profile = DEFAULT_PROFILE,
        policy: Policy = DEFAULT_POLICY,
        limits: Limits = DEFAULT_LIMITS,
        on_event: collections.abc.Callable[[Event], None] | None = None,
        record_events: bool = False,
        recorder: FlightRecorder | None = None,
    ) -> Terminal:
        return Terminal(
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            profile=profile,
            policy=policy,
            limits=limits,
            on_event=on_event,
            record_events=record_events,
            recorder=recorder,
        )

    return _make


@pytest.fixture
def vt_parser() -> Parser:
    """Return a fresh streaming :class:`~libvt.parser.Parser`."""
    from libvt.parser import Parser

    return Parser()


@pytest.fixture
def vt_pty() -> PtyFactory:
    """Return a POSIX :func:`~libvt.aio.open_pty` factory (skips on Windows)."""
    if sys.platform == "win32":
        pytest.skip("vt_pty requires POSIX pty.openpty()")

    from libvt.aio import open_pty

    def _spawn(
        argv: collections.abc.Sequence[str],
        *,
        rows: int = 24,
        cols: int = 80,
        env: collections.abc.Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> contextlib.AbstractAsyncContextManager[PtySession]:
        return open_pty(argv, rows=rows, cols=cols, env=env, cwd=cwd)

    return _spawn


__all__ = [
    "PtyFactory",
    "TerminalFactory",
    "vt_factory",
    "vt_parser",
    "vt_pty",
    "vt_recorder",
    "vt_terminal",
]
