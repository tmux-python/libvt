"""Tests for ``libvt.exc`` exception hierarchy, notes, and pickling."""

from __future__ import annotations

import copy
import errno
import pickle
import sys
import traceback

import pytest

from libvt.exc import TerminalTimeoutError, _reconstruct_timeout_error
from libvt.parser import ParserState


class _SlottedTimeoutError(TerminalTimeoutError):
    """Subclass with ``__slots__`` and custom keyword-only ``__init__``."""

    __slots__ = ("attempt",)

    def __init__(
        self,
        message: str | None = None,
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


class _CustomSigTimeoutError(TerminalTimeoutError):
    """Subclass with required positional arguments mismatching ``self.args``."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(f"{host}:{port}", eof=True)
        self.host = host
        self.port = port


class _ErrnoTimeoutError(TerminalTimeoutError):
    """Subclass populating C ``OSError`` ``errno``, ``strerror``, and filenames."""

    def __init__(
        self,
        code: int,
        strerror: str,
        filename: str | None = None,
        filename2: str | None = None,
    ) -> None:
        super().__init__(strerror, eof=True)
        if filename2 is not None:
            OSError.__init__(self, code, strerror, filename, None, filename2)
        elif filename is not None:
            OSError.__init__(self, code, strerror, filename)
        else:
            OSError.__init__(self, code, strerror)


class _DynamicTimeoutError(TerminalTimeoutError):
    """Subclass allowing dynamic ``__dict__`` attributes."""

    extra_context: str = ""


class _FalsyArgs(tuple[object, ...]):
    """Tuple subclass returning ``False`` from ``__bool__`` while non-empty."""

    def __bool__(self) -> bool:
        return False


_UNDERSCORE_SLOT_CLS = type(
    "___",
    (TerminalTimeoutError,),
    {"__module__": __name__, "__slots__": ("__raw_tag",)},
)
globals()["___"] = _UNDERSCORE_SLOT_CLS


def test_timeout_error_zero_arg_and_attach_note_suppression() -> None:
    """Zero-arg construction keeps ``args == ()``; ``attach_note=False`` omits notes."""
    raw_buf = bytearray(b"\x1b[31m")
    buf_exc = TerminalTimeoutError(recent_bytes=raw_buf)
    raw_buf[0] ^= 0xFF
    assert buf_exc.recent_bytes == b"\x1b[31m"

    empty = TerminalTimeoutError()
    assert isinstance(empty, TimeoutError)
    assert empty.args == ()
    assert str(empty) == ""
    assert empty.errno is None
    empty.add_note("zero-arg-note")
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        empty_rt = pickle.loads(pickle.dumps(empty, protocol=proto))
        assert type(empty_rt) is TerminalTimeoutError
        assert empty_rt.args == ()
        assert empty_rt.errno is None
        assert empty_rt.__notes__ == ["zero-arg-note"]

    falsy_rebuilt = _reconstruct_timeout_error(
        TerminalTimeoutError,
        _FalsyArgs(("stalled",)),
    )
    assert falsy_rebuilt.args == ("stalled",)

    if hasattr(sys, "monitoring"):
        calls: list[object] = []
        tool_id = next(tid for tid in range(6) if sys.monitoring.get_tool(tid) is None)
        sys.monitoring.use_tool_id(tool_id, "libvt_test_exc")
        try:
            sys.monitoring.register_callback(
                tool_id,
                sys.monitoring.events.CALL,
                lambda _code, _off, fn, _arg0: calls.append(fn),
            )
            sys.monitoring.set_local_events(
                tool_id,
                _reconstruct_timeout_error.__code__,
                sys.monitoring.events.CALL,
            )
            rebuilt_empty = _reconstruct_timeout_error(TerminalTimeoutError, ())
        finally:
            sys.monitoring.set_local_events(
                tool_id,
                _reconstruct_timeout_error.__code__,
                0,
            )
            sys.monitoring.register_callback(
                tool_id,
                sys.monitoring.events.CALL,
                None,
            )
            sys.monitoring.free_tool_id(tool_id)
        assert type(rebuilt_empty) is TerminalTimeoutError
        assert OSError.__init__ in calls

    uslot = _UNDERSCORE_SLOT_CLS("underscore-slot")
    setattr(uslot, "__raw_tag", "kept")
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        uslot_rt = pickle.loads(pickle.dumps(uslot, protocol=proto))
        assert getattr(uslot_rt, "__raw_tag") == "kept"
    for clone_fn in (copy.copy, copy.deepcopy):
        uslot_cp = clone_fn(uslot)
        assert getattr(uslot_cp, "__raw_tag") == "kept"

    quiet = TerminalTimeoutError(
        "quiet timeout",
        screen_text="secret-viewport",
        attach_note=False,
    )
    assert not hasattr(quiet, "__notes__")
    assert "secret-viewport" in quiet.format_diagnostics()
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        quiet_rt = pickle.loads(pickle.dumps(quiet, protocol=proto))
        assert not hasattr(quiet_rt, "__notes__")
        assert quiet_rt.screen_text == "secret-viewport"
    for clone_fn in (copy.copy, copy.deepcopy):
        quiet_cp = clone_fn(quiet)
        assert not hasattr(quiet_cp, "__notes__")
        assert quiet_cp.screen_text == "secret-viewport"

    quiet.add_note("late-note")
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        quiet_late = pickle.loads(pickle.dumps(quiet, protocol=proto))
        assert quiet_late.__notes__ == ["late-note"]
        quiet_late.add_note("clone-only")
        assert quiet.__notes__ == ["late-note"]
    for clone_fn in (copy.copy, copy.deepcopy):
        quiet_late_cp = clone_fn(quiet)
        assert quiet_late_cp.__notes__ == ["late-note"]
        quiet_late_cp.add_note("clone-only")
        assert quiet.__notes__ == ["late-note"]


def test_timeout_error_slots_subclasses_copy_pickle_and_exception_group() -> None:
    """``TerminalTimeoutError`` preserves slots, args, descriptors, and notes."""
    slotted = _SlottedTimeoutError(
        "slotted timeout",
        attempt=4,
        parser_state=ParserState.CSI_PARAM,
        screen_text="row0",
        recent_bytes=b"\x1b[1;",
    )
    slotted.add_note("slot-note")
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        slotted_rt = pickle.loads(pickle.dumps(slotted, protocol=proto))
        assert type(slotted_rt) is _SlottedTimeoutError
        assert slotted_rt.attempt == 4
        assert slotted_rt.errno is None
        assert slotted_rt.__notes__ == slotted.__notes__
    for cloned_slot in (copy.copy(slotted), copy.deepcopy(slotted)):
        assert type(cloned_slot) is _SlottedTimeoutError
        assert cloned_slot.attempt == 4
        assert cloned_slot.__notes__ == slotted.__notes__
        cloned_slot.add_note("slot-clone-only")
        assert "slot-clone-only" not in slotted.__notes__

    custom_sig = _CustomSigTimeoutError("127.0.0.1", 2222)
    custom_sig.add_note("sig-note")
    errno_2 = _ErrnoTimeoutError(errno.ETIMEDOUT, "PTY stalled")
    errno_3 = _ErrnoTimeoutError(errno.ETIMEDOUT, "PTY stalled", "/dev/pts/9")
    errno_3.add_note("errno-note")
    errno_5 = _ErrnoTimeoutError(
        errno.EXDEV,
        "Cross-device link",
        "/dev/pts/9",
        "/dev/pts/10",
    )
    slotted_empty = _SlottedTimeoutError(attempt=9)
    slotted_empty.add_note("slot-empty-note")

    multi_args = TerminalTimeoutError("initial")
    multi_args.args = ("%1", "127.0.0.1", 2222)

    one_arg_os = TerminalTimeoutError("stalled")
    one_arg_os.errno = errno.ETIMEDOUT
    one_arg_os.filename = "/dev/pts/0"
    one_arg_os.filename2 = "/dev/pts/1"

    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        cs_rt = pickle.loads(pickle.dumps(custom_sig, protocol=proto))
        assert type(cs_rt) is _CustomSigTimeoutError
        assert (cs_rt.host, cs_rt.port, cs_rt.args, cs_rt.eof) == (
            "127.0.0.1",
            2222,
            ("127.0.0.1:2222",),
            True,
        )
        assert cs_rt.__notes__ == custom_sig.__notes__

        e2_rt = pickle.loads(pickle.dumps(errno_2, protocol=proto))
        assert (e2_rt.errno, e2_rt.strerror, e2_rt.filename) == (
            errno.ETIMEDOUT,
            "PTY stalled",
            None,
        )

        er_rt = pickle.loads(pickle.dumps(errno_3, protocol=proto))
        assert type(er_rt) is _ErrnoTimeoutError
        assert (er_rt.errno, er_rt.strerror, er_rt.filename) == (
            errno.ETIMEDOUT,
            "PTY stalled",
            "/dev/pts/9",
        )
        assert er_rt.__notes__ == errno_3.__notes__

        e5_rt = pickle.loads(pickle.dumps(errno_5, protocol=proto))
        assert (e5_rt.errno, e5_rt.strerror, e5_rt.filename, e5_rt.filename2) == (
            errno.EXDEV,
            "Cross-device link",
            "/dev/pts/9",
            "/dev/pts/10",
        )

        se_rt = pickle.loads(pickle.dumps(slotted_empty, protocol=proto))
        assert (type(se_rt), se_rt.args, se_rt.attempt, se_rt.errno) == (
            _SlottedTimeoutError,
            (),
            9,
            None,
        )
        assert se_rt.__notes__ == slotted_empty.__notes__

        ma_rt = pickle.loads(pickle.dumps(multi_args, protocol=proto))
        assert ma_rt.args == ("%1", "127.0.0.1", 2222)
        assert (ma_rt.errno, ma_rt.strerror, ma_rt.filename) == (None, None, None)

        oa_rt = pickle.loads(pickle.dumps(one_arg_os, protocol=proto))
        assert oa_rt.args == ("stalled",)
        assert (oa_rt.errno, oa_rt.strerror, oa_rt.filename, oa_rt.filename2) == (
            errno.ETIMEDOUT,
            None,
            "/dev/pts/0",
            "/dev/pts/1",
        )

    for clone_fn in (copy.copy, copy.deepcopy):
        cs_cp = clone_fn(custom_sig)
        cs_cp.add_note("mutated")
        assert "mutated" not in custom_sig.__notes__
        assert (cs_cp.host, cs_cp.port) == ("127.0.0.1", 2222)

        e5_cp = clone_fn(errno_5)
        assert (e5_cp.errno, e5_cp.strerror, e5_cp.filename, e5_cp.filename2) == (
            errno.EXDEV,
            "Cross-device link",
            "/dev/pts/9",
            "/dev/pts/10",
        )

        ma_cp = clone_fn(multi_args)
        assert (ma_cp.args, ma_cp.errno) == (("%1", "127.0.0.1", 2222), None)

    eg_orig = ExceptionGroup("multi", [custom_sig, errno_3])
    eg_derived = eg_orig.derive([custom_sig])
    assert eg_derived.exceptions[0].__notes__ == custom_sig.__notes__
    eg_rt = pickle.loads(pickle.dumps(eg_orig, protocol=pickle.HIGHEST_PROTOCOL))
    assert type(eg_rt.exceptions[0]) is _CustomSigTimeoutError
    assert type(eg_rt.exceptions[1]) is _ErrnoTimeoutError
    eg_dc = copy.deepcopy(eg_orig)
    eg_dc.exceptions[0].add_note("dc-only")
    assert "dc-only" not in custom_sig.__notes__

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

    del restored.__notes__
    del_rt = pickle.loads(pickle.dumps(restored, protocol=pickle.HIGHEST_PROTOCOL))
    assert not hasattr(del_rt, "__notes__")

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


def test_timeout_error_setstate_validation_and_descriptors() -> None:
    """``__setstate__`` keeps ``None`` as a no-op, validates dicts, and sets causes."""
    live = TerminalTimeoutError(
        "live",
        parser_state=ParserState.CSI_PARAM,
        screen_text="hello",
    )
    live.__setstate__(None)
    assert live.screen_text == "hello"
    assert live.format_diagnostics() != ""

    for bad_state in (
        "corrupt",
        123,
        (1, 2, 3),
        ("bad", {}),
        ({}, "bad"),
        (None, "bad"),
        {123: "non-str-key"},
        {"screen_text": "wiped", 99: "bad"},
        ({"screen_text": "wiped"}, {99: "bad"}),
    ):
        with pytest.raises(TypeError):
            live.__setstate__(bad_state)
        assert live.screen_text == "hello"

    cause = KeyError("root")
    live.__setstate__({"__cause__": cause})
    assert live.__cause__ is cause
    assert "__cause__" not in live.__dict__
