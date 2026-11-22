"""Clean-room 14-state VT500 streaming byte parser and SGR parameter decoder.

State transitions follow Paul Flo Williams' public DEC ANSI parser state
diagram (ECMA-48 / DEC STD 070) with UTF-8 multi-byte decoding and bounded
parameter / string buffers matching modern terminal defenses.
"""

from __future__ import annotations

import codecs
import collections.abc
import enum
import re
import typing as t
from dataclasses import dataclass

from libvt.style import (
    DEFAULT_STYLE,
    AnsiColor,
    Color,
    IndexedColor,
    RgbColor,
    Style,
    UnderlineStyle,
)

MAX_CSI_PARAMS = 32
MAX_INTERMEDIATES = 4
MAX_OSC_BYTES = 16_384
MAX_DCS_BYTES = 1_048_576
MAX_APC_BYTES = 1_048_576
_PARAM_CLAMP = 65_535
_SGR_CACHE_LIMIT = 512
_CTRL_RE = re.compile(rb"[\x00-\x1f\x7f]")
_CSI_FAST_RE = re.compile(rb"([\x3c-\x3f]?)([0-9;:]*)([\x40-\x7e])")


class ParserState(enum.IntEnum):
    """States of the DEC ANSI / VT500 byte state machine."""

    GROUND = 0
    ESCAPE = 1
    ESCAPE_INTERMEDIATE = 2
    CSI_ENTRY = 3
    CSI_PARAM = 4
    CSI_INTERMEDIATE = 5
    CSI_IGNORE = 6
    DCS_ENTRY = 7
    DCS_PARAM = 8
    DCS_INTERMEDIATE = 9
    DCS_PASSTHROUGH = 10
    DCS_IGNORE = 11
    OSC_STRING = 12
    SOS_PM_APC_STRING = 13


@dataclass(frozen=True, slots=True)
class CsiParams:
    """Parsed CSI parameter list preserving ``;`` vs ``:`` subparameter boundaries.

    Attributes
    ----------
    values : tuple[int, ...]
        Clamped integer parameter values (``0`` for omitted slots).
    colon_mask : int
        Bitmask where bit ``i`` is ``1`` when ``values[i]`` was preceded by
        ``:`` rather than ``;``.
    omitted_mask : int
        Bitmask where bit ``i`` is ``1`` when ``values[i]`` had no digits.
    """

    values: tuple[int, ...] = ()
    colon_mask: int = 0
    omitted_mask: int = 0

    def get(self, index: int, default: int = 0) -> int:
        """Return parameter at *index*, using *default* when omitted or 0-defaulted."""
        if index >= len(self.values):
            return default
        if self.omitted_mask & (1 << index):
            return default
        val = self.values[index]
        return default if val == 0 and default != 0 else val

    def raw_or_default(self, index: int, default: int) -> int:
        """Return parameter at *index*, using *default* only when omitted."""
        if index >= len(self.values) or (self.omitted_mask & (1 << index)):
            return default
        return self.values[index]

    def groups(self) -> list[tuple[int, ...]]:
        """Group parameters by semicolon-delimited slots with colon subparameters."""
        if not self.values:
            return []
        result: list[list[int]] = [[self.values[0]]]
        for idx in range(1, len(self.values)):
            if self.colon_mask & (1 << idx):
                result[-1].append(self.values[idx])
            else:
                result.append([self.values[idx]])
        return [tuple(g) for g in result]


@dataclass(frozen=True, slots=True)
class Print:
    """Printable text run emitted from ``GROUND``.

    Attributes
    ----------
    text : str
        Decoded Unicode text segment (one or more printable characters).
    """

    text: str


@dataclass(frozen=True, slots=True)
class Execute:
    """C0 or C1 control character executed immediately.

    Attributes
    ----------
    byte : int
        Control byte value (e.g. ``0x07`` BEL, ``0x08`` BS, ``0x0A`` LF).
    """

    byte: int


@dataclass(frozen=True, slots=True)
class EscDispatch:
    """Completed two- or multi-byte ``ESC`` sequence.

    Attributes
    ----------
    intermediates : bytes
        Intermediate bytes in ``0x20..0x2F``.
    final : int
        Final byte in ``0x30..0x7E``.
    """

    intermediates: bytes
    final: int


@dataclass(frozen=True, slots=True)
class CsiDispatch:
    """Completed ``CSI`` control sequence.

    Attributes
    ----------
    prefix : int
        Private marker byte in ``0x3C..0x3F`` (e.g. ``ord('?')``), or ``0``.
    params : CsiParams
        Numeric parameters and colon/omission bitmasks.
    intermediates : bytes
        Intermediate bytes in ``0x20..0x2F``.
    final : int
        Final byte in ``0x40..0x7E``.
    """

    prefix: int
    params: CsiParams
    intermediates: bytes
    final: int


@dataclass(frozen=True, slots=True)
class OscDispatch:
    """Completed Operating System Command (``OSC``) string.

    Attributes
    ----------
    code : int
        Numeric OSC command identifier, or ``-1`` when absent.
    data : bytes
        Payload bytes following the initial ``;`` separator.
    """

    code: int
    data: bytes


@dataclass(frozen=True, slots=True)
class DcsDispatch:
    """Completed Device Control String (``DCS``) payload.

    Attributes
    ----------
    prefix : int
        Private marker byte in ``0x3C..0x3F``, or ``0``.
    params : CsiParams
        Header numeric parameters.
    intermediates : bytes
        Header intermediate bytes.
    final : int
        Header final byte in ``0x40..0x7E``.
    data : bytes
        Passthrough payload bytes up to ``ST``.
    """

    prefix: int
    params: CsiParams
    intermediates: bytes
    final: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ApcDispatch:
    """Completed Application Program Command (``APC``) payload.

    Attributes
    ----------
    data : bytes
        Payload bytes between ``ESC _`` and ``ST``.
    """

    data: bytes


type Action = (
    Print
    | Execute
    | EscDispatch
    | CsiDispatch
    | OscDispatch
    | DcsDispatch
    | ApcDispatch
)


class ActionSink(t.Protocol):
    """Callback protocol receiving semantic parser events without boxing ``Print``."""

    def on_print(self, text: str) -> None:
        """Handle printable Unicode text."""

    def on_execute(self, byte: int) -> None:
        """Handle a C0 control byte."""

    def on_esc(self, intermediates: bytes, final: int) -> None:
        """Handle a completed ``ESC`` sequence."""

    def on_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        """Handle a completed ``CSI`` sequence."""

    def on_osc(self, code: int, data: bytes) -> None:
        """Handle a completed ``OSC`` sequence."""

    def on_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        """Handle a completed ``DCS`` sequence."""

    def on_apc(self, data: bytes) -> None:
        """Handle a completed ``APC`` sequence."""


class _ListSink:
    """Internal sink collecting boxed :data:`Action` instances."""

    __slots__ = ("actions",)

    def __init__(self) -> None:
        self.actions: list[Action] = []

    def on_print(self, text: str) -> None:
        self.actions.append(Print(text))

    def on_execute(self, byte: int) -> None:
        self.actions.append(Execute(byte))

    def on_esc(self, intermediates: bytes, final: int) -> None:
        self.actions.append(EscDispatch(intermediates, final))

    def on_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        self.actions.append(CsiDispatch(prefix, params, intermediates, final))

    def on_osc(self, code: int, data: bytes) -> None:
        self.actions.append(OscDispatch(code, data))

    def on_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        self.actions.append(
            DcsDispatch(prefix, params, intermediates, final, data),
        )

    def on_apc(self, data: bytes) -> None:
        self.actions.append(ApcDispatch(data))


_sgr_memo: dict[tuple[Style, CsiParams], Style] = {}
_csi_param_memo: dict[bytes, CsiParams] = {}
_EMPTY_PARAMS = CsiParams()


def _parse_fast_csi_params(raw: bytes) -> CsiParams:
    """Decode ASCII CSI parameter bytes ``[0-9;:]*`` with memoization."""
    if not raw:
        return _EMPTY_PARAMS
    cached = _csi_param_memo.get(raw)
    if cached is not None:
        return cached
    values: list[int] = []
    colon_mask = 0
    omitted_mask = 0
    cur = 0
    has_digit = False
    for b in raw:
        if 0x30 <= b <= 0x39:
            cur = min(_PARAM_CLAMP, cur * 10 + (b - 0x30))
            has_digit = True
        else:
            idx = len(values)
            if idx < MAX_CSI_PARAMS:
                values.append(cur if has_digit else 0)
                if not has_digit:
                    omitted_mask |= 1 << idx
                if b == 0x3A:
                    colon_mask |= 1 << (idx + 1)
            cur = 0
            has_digit = False
    idx = len(values)
    if idx < MAX_CSI_PARAMS:
        values.append(cur if has_digit else 0)
        if not has_digit:
            omitted_mask |= 1 << idx
    res = CsiParams(
        values=tuple(values),
        colon_mask=colon_mask,
        omitted_mask=omitted_mask,
    )
    if len(_csi_param_memo) < _SGR_CACHE_LIMIT:
        _csi_param_memo[raw] = res
    return res


def _decode_extended_color(
    groups: list[tuple[int, ...]],
    idx: int,
) -> tuple[Color, int, bool]:
    """Decode ``38`` / ``48`` / ``58`` color sequences in colon or semicolon form."""
    grp = groups[idx]
    if len(grp) >= 2:
        mode = grp[1]
        if mode == 5 and len(grp) >= 3 and 0 <= grp[2] <= 255:
            return _palette_to_color(grp[2]), idx + 1, True
        if mode == 2:
            # ITU T.416 colon form may be 38:2:cs:r:g:b (6) or 38:2:r:g:b (5).
            channels = grp[3:6] if len(grp) >= 6 else grp[2:5]
            if len(channels) == 3 and all(0 <= c <= 255 for c in channels):
                return (
                    RgbColor(channels[0], channels[1], channels[2]),
                    idx + 1,
                    True,
                )
        return None, idx + 1, False
    if idx + 1 < len(groups):
        mode = groups[idx + 1][0]
        if mode == 5 and idx + 2 < len(groups):
            pal = groups[idx + 2][0]
            if 0 <= pal <= 255:
                return _palette_to_color(pal), idx + 3, True
            return None, idx + 3, False
        if mode == 2 and idx + 4 < len(groups):
            r = groups[idx + 2][0]
            g = groups[idx + 3][0]
            b = groups[idx + 4][0]
            if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
                return RgbColor(r, g, b), idx + 5, True
            return None, idx + 5, False
    return None, idx + 1, False


def _palette_to_color(index: int) -> Color:
    if 0 <= index <= 15:
        return AnsiColor(index)
    return IndexedColor(index)


def parse_sgr(params: CsiParams, current: Style = DEFAULT_STYLE) -> Style:
    """Apply ``CSI ... m`` SGR parameters onto *current* and return the new style."""
    if not params.values or (len(params.values) == 1 and params.values[0] == 0):
        return DEFAULT_STYLE
    cache_key = (current, params)
    cached = _sgr_memo.get(cache_key)
    if cached is not None:
        return cached

    fg = current.fg
    bg = current.bg
    ul_color = current.underline_color
    bold = current.bold
    dim = current.dim
    italic = current.italic
    underline = current.underline
    blink = current.blink
    reverse = current.reverse
    hidden = current.hidden
    strike = current.strikethrough
    overline = current.overline

    groups = params.groups()
    idx = 0
    while idx < len(groups):
        grp = groups[idx]
        code = grp[0]
        if code == 0:
            fg = None
            bg = None
            ul_color = None
            bold = dim = italic = blink = reverse = hidden = strike = overline = False
            underline = UnderlineStyle.NONE
        elif code == 1:
            bold = True
        elif code == 2:
            dim = True
        elif code == 3:
            italic = True
        elif code == 4:
            if len(grp) >= 2:
                sub = grp[1]
                underline = (
                    UnderlineStyle(sub) if 0 <= sub <= 5 else UnderlineStyle.SINGLE
                )
            else:
                underline = UnderlineStyle.SINGLE
        elif code in (5, 6):
            blink = True
        elif code == 7:
            reverse = True
        elif code == 8:
            hidden = True
        elif code == 9:
            strike = True
        elif code == 21:
            underline = UnderlineStyle.DOUBLE
        elif code == 22:
            bold = False
            dim = False
        elif code == 23:
            italic = False
        elif code == 24:
            underline = UnderlineStyle.NONE
        elif code == 25:
            blink = False
        elif code == 27:
            reverse = False
        elif code == 28:
            hidden = False
        elif code == 29:
            strike = False
        elif 30 <= code <= 37:
            fg = AnsiColor(code - 30)
        elif code == 38:
            color, next_idx, ok = _decode_extended_color(groups, idx)
            if ok:
                fg = color
            idx = next_idx
            continue
        elif code == 39:
            fg = None
        elif 40 <= code <= 47:
            bg = AnsiColor(code - 40)
        elif code == 48:
            color, next_idx, ok = _decode_extended_color(groups, idx)
            if ok:
                bg = color
            idx = next_idx
            continue
        elif code == 49:
            bg = None
        elif code == 53:
            overline = True
        elif code == 55:
            overline = False
        elif code == 58:
            color, next_idx, ok = _decode_extended_color(groups, idx)
            if ok:
                ul_color = color
            idx = next_idx
            continue
        elif code == 59:
            ul_color = None
        elif 90 <= code <= 97:
            fg = AnsiColor(code - 90 + 8)
        elif 100 <= code <= 107:
            bg = AnsiColor(code - 100 + 8)
        idx += 1

    result = Style(
        fg=fg,
        bg=bg,
        underline_color=ul_color,
        bold=bold,
        dim=dim,
        italic=italic,
        underline=underline,
        blink=blink,
        reverse=reverse,
        hidden=hidden,
        strikethrough=strike,
        overline=overline,
    )
    if len(_sgr_memo) >= _SGR_CACHE_LIMIT:
        _sgr_memo.clear()
    _sgr_memo[cache_key] = result
    return result


class Parser:
    """Streaming 14-state VT500 byte parser with partial UTF-8 retention."""

    __slots__ = (
        "_csi_colon_mask",
        "_csi_cur",
        "_csi_has_digit",
        "_csi_omitted_mask",
        "_csi_overflow",
        "_csi_prefix",
        "_csi_values",
        "_dcs_final",
        "_intermediates",
        "_return_from_esc_st",
        "_state",
        "_str_buf",
        "_str_kind",
        "_str_overflow",
        "_utf8",
    )

    def __init__(self) -> None:
        self._state: ParserState = ParserState.GROUND
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._intermediates = bytearray()
        self._csi_prefix: int = 0
        self._csi_values: list[int] = []
        self._csi_colon_mask: int = 0
        self._csi_omitted_mask: int = 0
        self._csi_cur: int = 0
        self._csi_has_digit: bool = False
        self._csi_overflow: bool = False
        self._dcs_final: int = 0
        self._str_buf = bytearray()
        self._str_kind: str = ""
        self._str_overflow: bool = False
        self._return_from_esc_st: ParserState | None = None

    @property
    def state(self) -> ParserState:
        """Return current parser state."""
        return self._state

    def reset(self) -> None:
        """Reset parser state and discard buffered incomplete sequences."""
        self._state = ParserState.GROUND
        self._utf8.reset()
        self._clear_csi()
        self._str_buf.clear()
        self._str_kind = ""
        self._str_overflow = False
        self._return_from_esc_st = None

    def _clear_csi(self) -> None:
        self._intermediates.clear()
        self._csi_prefix = 0
        self._csi_values.clear()
        self._csi_colon_mask = 0
        self._csi_omitted_mask = 0
        self._csi_cur = 0
        self._csi_has_digit = False
        self._csi_overflow = False
        self._dcs_final = 0

    def _push_param(self, *, next_is_colon: bool = False) -> None:
        idx = len(self._csi_values)
        if idx < MAX_CSI_PARAMS:
            self._csi_values.append(self._csi_cur if self._csi_has_digit else 0)
            if not self._csi_has_digit:
                self._csi_omitted_mask |= 1 << idx
            if next_is_colon:
                self._csi_colon_mask |= 1 << (idx + 1)
        else:
            self._csi_overflow = True
        self._csi_cur = 0
        self._csi_has_digit = False

    def _finalize_params(self) -> CsiParams:
        if (
            self._csi_has_digit
            or self._csi_values
            or self._csi_colon_mask
            or self._csi_omitted_mask
        ):
            self._push_param()
        return CsiParams(
            values=tuple(self._csi_values),
            colon_mask=self._csi_colon_mask,
            omitted_mask=self._csi_omitted_mask,
        )

    def feed(self, data: collections.abc.Buffer) -> list[Action]:
        """Parse *data* and return semantic :data:`Action` events."""
        sink = _ListSink()
        self.feed_to(data, sink)
        return sink.actions

    def feed_to(
        self,
        data: collections.abc.Buffer,
        sink: ActionSink,
    ) -> None:
        """Parse *data* and stream events directly to *sink*."""
        buf = data if isinstance(data, bytes) else bytes(data)
        n = len(buf)
        i = 0
        while i < n:
            state = self._state
            if state == ParserState.GROUND:
                ctrl_match = _CTRL_RE.search(buf, i)
                end = ctrl_match.start() if ctrl_match is not None else n
                if end > i:
                    chunk = buf[i:end]
                    if chunk.isascii() and not self._utf8.getstate()[0]:
                        sink.on_print(chunk.decode("ascii"))
                    else:
                        decoded = self._utf8.decode(chunk, final=False)
                        if decoded:
                            sink.on_print(decoded)
                    i = end
                    if i >= n:
                        break
                b = buf[i]
                i += 1
                if (b == 0x1B or b in (0x18, 0x1A)) and self._utf8.getstate()[0]:
                    flushed = self._utf8.decode(b"", final=True)
                    if flushed:
                        sink.on_print(flushed)
                if b == 0x1B:
                    if i < n and buf[i] == 0x5B:  # ESC [ -> CSI fast path
                        i += 1
                        m = _CSI_FAST_RE.match(buf, i)
                        if m is not None:
                            pfx_b, raw_p, fin_b = m.groups()
                            pfx = pfx_b[0] if pfx_b else 0
                            params = _parse_fast_csi_params(raw_p)
                            sink.on_csi(pfx, params, b"", fin_b[0])
                            i = m.end()
                            continue
                        self._clear_csi()
                        self._state = ParserState.CSI_ENTRY
                        continue
                    self._clear_csi()
                    self._state = ParserState.ESCAPE
                elif b in (0x18, 0x1A):
                    self._state = ParserState.GROUND
                elif b < 0x20:
                    sink.on_execute(b)
                continue

            b = buf[i]
            i += 1

            # Anywhere transitions inside non-string states (ECMA-48 / VT500).
            if state not in (
                ParserState.OSC_STRING,
                ParserState.DCS_PASSTHROUGH,
                ParserState.DCS_IGNORE,
                ParserState.SOS_PM_APC_STRING,
            ):
                if b == 0x1B:
                    self._clear_csi()
                    self._state = ParserState.ESCAPE
                    continue
                if b in (0x18, 0x1A):
                    self._state = ParserState.GROUND
                    continue
                if b < 0x20:
                    sink.on_execute(b)
                    continue
                if b == 0x7F:
                    continue

            if state == ParserState.ESCAPE:
                if self._return_from_esc_st is not None:
                    prev_state = self._return_from_esc_st
                    self._return_from_esc_st = None
                    if b == 0x5C:  # ESC \ (ST)
                        self._finish_string(prev_state, sink)
                        self._state = ParserState.GROUND
                        continue
                    # ESC started a fresh escape sequence; abort prior string.
                    self._str_buf.clear()
                    self._str_overflow = False

                if b == 0x5B:  # '[' -> CSI
                    self._clear_csi()
                    self._state = ParserState.CSI_ENTRY
                elif b == 0x5D:  # ']' -> OSC
                    self._str_buf.clear()
                    self._str_kind = "osc"
                    self._str_overflow = False
                    self._state = ParserState.OSC_STRING
                elif b == 0x50:  # 'P' -> DCS
                    self._clear_csi()
                    self._str_buf.clear()
                    self._str_kind = "dcs"
                    self._str_overflow = False
                    self._state = ParserState.DCS_ENTRY
                elif b == 0x5F:  # '_' -> APC
                    self._str_buf.clear()
                    self._str_kind = "apc"
                    self._str_overflow = False
                    self._state = ParserState.SOS_PM_APC_STRING
                elif b in (0x58, 0x5E):  # 'X' SOS, '^' PM
                    self._str_buf.clear()
                    self._str_kind = "ignore"
                    self._str_overflow = True
                    self._state = ParserState.SOS_PM_APC_STRING
                elif 0x20 <= b <= 0x2F:
                    self._intermediates.append(b)
                    self._state = ParserState.ESCAPE_INTERMEDIATE
                elif 0x30 <= b <= 0x7E:
                    sink.on_esc(bytes(self._intermediates), b)
                    self._state = ParserState.GROUND
                else:
                    self._state = ParserState.GROUND

            elif state == ParserState.ESCAPE_INTERMEDIATE:
                if 0x20 <= b <= 0x2F:
                    if len(self._intermediates) < MAX_INTERMEDIATES:
                        self._intermediates.append(b)
                elif 0x30 <= b <= 0x7E:
                    sink.on_esc(bytes(self._intermediates), b)
                    self._state = ParserState.GROUND
                else:
                    self._state = ParserState.GROUND

            elif state in (ParserState.CSI_ENTRY, ParserState.CSI_PARAM):
                if state == ParserState.CSI_ENTRY and 0x3C <= b <= 0x3F:
                    self._csi_prefix = b
                    self._state = ParserState.CSI_PARAM
                elif 0x30 <= b <= 0x39:
                    self._csi_cur = min(
                        _PARAM_CLAMP,
                        self._csi_cur * 10 + (b - 0x30),
                    )
                    self._csi_has_digit = True
                    self._state = ParserState.CSI_PARAM
                elif b == 0x3B:  # ';'
                    self._push_param(next_is_colon=False)
                    self._state = ParserState.CSI_PARAM
                elif b == 0x3A:  # ':'
                    self._push_param(next_is_colon=True)
                    self._state = ParserState.CSI_PARAM
                elif 0x20 <= b <= 0x2F:
                    self._intermediates.append(b)
                    self._state = ParserState.CSI_INTERMEDIATE
                elif 0x40 <= b <= 0x7E:
                    params = self._finalize_params()
                    sink.on_csi(
                        self._csi_prefix,
                        params,
                        bytes(self._intermediates),
                        b,
                    )
                    self._state = ParserState.GROUND
                else:
                    self._state = ParserState.CSI_IGNORE

            elif state == ParserState.CSI_INTERMEDIATE:
                if 0x20 <= b <= 0x2F:
                    if len(self._intermediates) < MAX_INTERMEDIATES:
                        self._intermediates.append(b)
                elif 0x40 <= b <= 0x7E:
                    params = self._finalize_params()
                    sink.on_csi(
                        self._csi_prefix,
                        params,
                        bytes(self._intermediates),
                        b,
                    )
                    self._state = ParserState.GROUND
                else:
                    self._state = ParserState.CSI_IGNORE

            elif state == ParserState.CSI_IGNORE:
                if 0x40 <= b <= 0x7E:
                    self._state = ParserState.GROUND

            elif state in (ParserState.DCS_ENTRY, ParserState.DCS_PARAM):
                if state == ParserState.DCS_ENTRY and 0x3C <= b <= 0x3F:
                    self._csi_prefix = b
                    self._state = ParserState.DCS_PARAM
                elif 0x30 <= b <= 0x39:
                    self._csi_cur = min(
                        _PARAM_CLAMP,
                        self._csi_cur * 10 + (b - 0x30),
                    )
                    self._csi_has_digit = True
                    self._state = ParserState.DCS_PARAM
                elif b == 0x3B:
                    self._push_param(next_is_colon=False)
                    self._state = ParserState.DCS_PARAM
                elif b == 0x3A:
                    self._push_param(next_is_colon=True)
                    self._state = ParserState.DCS_PARAM
                elif 0x20 <= b <= 0x2F:
                    self._intermediates.append(b)
                    self._state = ParserState.DCS_INTERMEDIATE
                elif 0x40 <= b <= 0x7E:
                    self._dcs_final = b
                    self._state = ParserState.DCS_PASSTHROUGH
                else:
                    self._state = ParserState.DCS_IGNORE

            elif state == ParserState.DCS_INTERMEDIATE:
                if 0x20 <= b <= 0x2F:
                    if len(self._intermediates) < MAX_INTERMEDIATES:
                        self._intermediates.append(b)
                elif 0x40 <= b <= 0x7E:
                    self._dcs_final = b
                    self._state = ParserState.DCS_PASSTHROUGH
                else:
                    self._state = ParserState.DCS_IGNORE

            elif state == ParserState.DCS_PASSTHROUGH:
                if b == 0x1B:
                    self._return_from_esc_st = ParserState.DCS_PASSTHROUGH
                    self._state = ParserState.ESCAPE
                elif b in (0x18, 0x1A):
                    self._str_buf.clear()
                    self._state = ParserState.GROUND
                elif b == 0x9C:  # 8-bit ST
                    self._finish_string(ParserState.DCS_PASSTHROUGH, sink)
                    self._state = ParserState.GROUND
                elif not self._str_overflow:
                    if len(self._str_buf) < MAX_DCS_BYTES:
                        self._str_buf.append(b)
                    else:
                        self._str_overflow = True

            elif state == ParserState.DCS_IGNORE:
                if b == 0x1B:
                    self._return_from_esc_st = ParserState.DCS_IGNORE
                    self._state = ParserState.ESCAPE
                elif b in (0x18, 0x1A, 0x9C):
                    self._state = ParserState.GROUND

            elif state == ParserState.OSC_STRING:
                if b == 0x07 or b == 0x9C:  # BEL or 8-bit ST
                    self._finish_string(ParserState.OSC_STRING, sink)
                    self._state = ParserState.GROUND
                elif b == 0x1B:
                    self._return_from_esc_st = ParserState.OSC_STRING
                    self._state = ParserState.ESCAPE
                elif b in (0x18, 0x1A):
                    self._str_buf.clear()
                    self._state = ParserState.GROUND
                elif (b >= 0x20 or b == 0x09) and not self._str_overflow:
                    if len(self._str_buf) < MAX_OSC_BYTES:
                        self._str_buf.append(b)
                    else:
                        self._str_overflow = True

            elif state == ParserState.SOS_PM_APC_STRING:
                if b == 0x1B:
                    self._return_from_esc_st = ParserState.SOS_PM_APC_STRING
                    self._state = ParserState.ESCAPE
                elif b in (0x18, 0x1A):
                    self._str_buf.clear()
                    self._state = ParserState.GROUND
                elif b == 0x9C:
                    self._finish_string(ParserState.SOS_PM_APC_STRING, sink)
                    self._state = ParserState.GROUND
                elif not self._str_overflow:
                    if len(self._str_buf) < MAX_APC_BYTES:
                        self._str_buf.append(b)
                    else:
                        self._str_overflow = True

    def _finish_string(self, from_state: ParserState, sink: ActionSink) -> None:
        if self._str_overflow:
            self._str_buf.clear()
            self._str_overflow = False
            return
        payload = bytes(self._str_buf)
        self._str_buf.clear()
        if from_state == ParserState.OSC_STRING:
            sep = payload.find(b";")
            if sep == -1:
                code = int(payload) if payload.isdigit() else -1
                data = b""
            else:
                head = payload[:sep]
                code = int(head) if head.isdigit() else -1
                data = payload[sep + 1 :]
            sink.on_osc(code, data)
        elif from_state == ParserState.DCS_PASSTHROUGH:
            params = self._finalize_params()
            sink.on_dcs(
                self._csi_prefix,
                params,
                bytes(self._intermediates),
                self._dcs_final,
                payload,
            )
        elif from_state == ParserState.SOS_PM_APC_STRING and self._str_kind == "apc":
            sink.on_apc(payload)


__all__ = [
    "MAX_APC_BYTES",
    "MAX_CSI_PARAMS",
    "MAX_DCS_BYTES",
    "MAX_INTERMEDIATES",
    "MAX_OSC_BYTES",
    "Action",
    "ActionSink",
    "ApcDispatch",
    "CsiDispatch",
    "CsiParams",
    "DcsDispatch",
    "EscDispatch",
    "Execute",
    "OscDispatch",
    "Parser",
    "ParserState",
    "Print",
    "parse_sgr",
]
