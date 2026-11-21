"""Asyncio stream adapter tests for ``libvt.aio.AsyncTerminalStream``."""

from __future__ import annotations

import asyncio

import pytest

from libvt.aio import AsyncTerminalStream
from libvt.exc import TerminalTimeoutError


async def test_async_stream_coalesces_sync_frame_and_waits() -> None:
    """``AsyncTerminalStream.wait_for`` holds during ``?2026`` until frame commit."""
    reader = asyncio.StreamReader()
    stream = AsyncTerminalStream(reader, rows=4, cols=20, sync_timeout=0.5)
    async with stream:
        reader.feed_data(b"\x1b[?2026h\x1b[2J\x1b[Htear-mid-frame")
        await asyncio.sleep(0.02)
        assert stream.terminal.committed.text == ""
        reader.feed_data(b"\r\nready!\x1b[?2026l")
        snap = await stream.wait_for("ready!", timeout=1.0)
        assert snap.text == "tear-mid-frame\nready!"
        reader.feed_eof()


async def test_async_stream_sync_timeout_releases_stuck_hold() -> None:
    """A stuck ``?2026`` batch releases after ``sync_timeout`` elapses."""
    reader = asyncio.StreamReader()
    stream = AsyncTerminalStream(reader, rows=4, cols=20, sync_timeout=0.05)
    async with stream:
        reader.feed_data(b"\x1b[?2026hstuck-but-visible")
        snap = await stream.wait_for("stuck-but-visible", timeout=0.5)
        assert "stuck-but-visible" in snap.text
        reader.feed_eof()


async def test_async_stream_timeout_raises_terminal_timeout_error() -> None:
    """``wait_for`` raises :exc:`TerminalTimeoutError` when predicate never matches."""
    reader = asyncio.StreamReader()
    stream = AsyncTerminalStream(reader, rows=4, cols=20)
    reader.feed_data(b"booting...")
    reader.feed_eof()
    with pytest.raises(TerminalTimeoutError):
        await stream.wait_for("never-arrives", timeout=0.05)
