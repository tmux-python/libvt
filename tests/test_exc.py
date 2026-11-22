"""Tests for ``libvt.exc`` exception hierarchy, notes, and pickling."""

from __future__ import annotations

import copy
import pickle
import traceback

from libvt.exc import TerminalTimeoutError
from libvt.parser import ParserState


class _SlottedTimeoutError(TerminalTimeoutError):
    """Subclass with ``__slots__`` and custom keyword-only ``__init__``."""

    __slots__ = ("attempt",)

    def __init__(
        self,
        message: str,
        *,
        attempt: int,
        parser_state: ParserState = ParserState.GROUND,
        screen_text: str = "",
        recent_bytes: bytes | bytearray | memoryview = b"",
    ) -> None:
        super().__init__(
            message,
            parser_state=parser_state,
            screen_text=screen_text,
            recent_bytes=recent_bytes,
            eof=False,
        )
        self.attempt = attempt


class _DynamicTimeoutError(TerminalTimeoutError):
    """Subclass allowing dynamic ``__dict__`` attributes."""

    extra_context: str = ""


def test_timeout_error_zero_arg_and_attach_note_suppression() -> None:
    """Zero-arg construction keeps ``args == ()``; ``attach_note=False`` omits notes."""
    empty = TerminalTimeoutError()
    assert isinstance(empty, TimeoutError)
    assert empty.args == ()
    assert str(empty) == ""
    assert empty.errno is None

    quiet = TerminalTimeoutError(
        "quiet timeout",
        screen_text="secret-viewport",
        attach_note=False,
    )
    assert not hasattr(quiet, "__notes__")
    assert "secret-viewport" in quiet.format_diagnostics()
    for proto in (0, pickle.HIGHEST_PROTOCOL):
        quiet_rt = pickle.loads(pickle.dumps(quiet, protocol=proto))
        assert not hasattr(quiet_rt, "__notes__")
        assert quiet_rt.screen_text == "secret-viewport"


def test_timeout_error_slots_subclasses_copy_pickle_and_exception_group() -> None:
    """``TerminalTimeoutError`` preserves slots, dynamic attrs, and unaliased notes."""
    slotted = _SlottedTimeoutError(
        "slotted timeout",
        attempt=4,
        parser_state=ParserState.CSI_PARAM,
        screen_text="row0",
        recent_bytes=b"\x1b[1;",
    )
    slotted.add_note("slot-note")
    for proto in (0, pickle.HIGHEST_PROTOCOL):
        slotted_rt = pickle.loads(pickle.dumps(slotted, protocol=proto))
        assert type(slotted_rt) is _SlottedTimeoutError
        assert slotted_rt.attempt == 4
        assert slotted_rt.errno is None
        assert slotted_rt.__notes__ == slotted.__notes__
    for cloned_slot in (copy.copy(slotted), copy.deepcopy(slotted)):
        assert type(cloned_slot) is _SlottedTimeoutError
        assert cloned_slot.attempt == 4
        assert cloned_slot.__notes__ == slotted.__notes__

    sub = _DynamicTimeoutError(
        "timed out waiting for prompt",
        parser_state=ParserState.OSC_STRING,
        screen_text="$ stuck",
        recent_bytes=memoryview(b"\x1b]0;unterminated"),
        eof=False,
    )
    sub.add_note("attempt=3")
    sub.extra_context = "pane-%1"

    assert str(sub) == "timed out waiting for prompt"
    assert "\n" not in str(sub)
    assert isinstance(sub.recent_bytes, bytes)
    assert any("OSC_STRING" in n for n in sub.__notes__)
    assert "parser_state=OSC_STRING" in "".join(traceback.format_exception(sub))

    shallow = copy.copy(sub)
    shallow.add_note("shallow-only")
    assert "shallow-only" not in sub.__notes__

    restored = pickle.loads(pickle.dumps(sub, protocol=pickle.HIGHEST_PROTOCOL))
    assert type(restored) is _DynamicTimeoutError
    assert restored.errno is None
    assert restored.parser_state == ParserState.OSC_STRING
    assert restored.recent_bytes == b"\x1b]0;unterminated"
    assert restored.__notes__ == sub.__notes__
    assert restored.extra_context == "pane-%1"

    caught: list[TerminalTimeoutError] = []
    group_msg = "batch"
    other_msg = "other"
    try:
        raise ExceptionGroup(group_msg, [sub, ValueError(other_msg)])
    except* TimeoutError as eg:
        caught.extend(e for e in eg.exceptions if isinstance(e, TerminalTimeoutError))
    except* ValueError:
        pass
    assert len(caught) == 1
    assert caught[0].__notes__ == sub.__notes__
