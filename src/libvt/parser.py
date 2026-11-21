"""Clean-room VT500 streaming byte parser and SGR parameter decoder.

State transitions follow Paul Flo Williams' public DEC ANSI parser state
diagram (ECMA-48 / DEC STD 070) with UTF-8 multi-byte decoding, 7-bit string
escape isolation via :attr:`ParserState.STRING_ESC`, optional legacy Latin-1
8-bit ``C1`` wire parsing, and bounded parameter and string buffers.
"""

from __future__ import annotations

import codecs
import collections.abc
import enum
import re
import typing as t
from dataclasses import dataclass

from libvt.config import DEFAULT_LIMITS, Limits
from libvt.style import (
    DEFAULT_STYLE,
    AnsiColor,
    Color,
    IndexedColor,
    RgbColor,
    Style,
    UnderlineStyle,
)

MAX_INTERMEDIATES = 4
_PARAM_CLAMP = 65_535
_MEMO_LIMIT = 256
_CTRL_RE = re.compile(rb"[\x00-\x1f\x7f]")
_CSI_FAST_RE = re.compile(rb"([\x3c-\x3f]?)([0-9;:]{0,256})([\x40-\x7e])")
_C1_CHAR_RE = re.compile(r"[\x80-\x9f]")

type ParserEncoding = t.Literal["utf-8", "latin-1"]


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
    STRING_ESC = 14


@dataclass(frozen=True, slots=True)
class CsiParams:
    """Parsed CSI parameter list preserving ``;`` vs ``:`` subparameter boundaries.

    Attributes
    ----------
    values : tuple[int, ...]
        Clamped integer parameter values (``0`` for omitted slots).
    colon_mask : int
        Bitmask where bit ``i`` is ``1`` when ``values[i]`` was preceded by
        ``:`` (an ITU T.416 subparameter of the nearest preceding ``;`` slot).
    omitted_mask : int
        Bitmask where bit ``i`` is ``1`` when slot ``i`` contained no digits.
    """

    values: tuple[int, ...] = ()
    colon_mask: int = 0
    omitted_mask: int = 0

    def get(self, index: int, default: int = 1) -> int:
        """Return parameter *index*, substituting *default* when omitted or ``0``."""
        if index >= len(self.values):
            return default
        val = self.values[index]
        return default if val == 0 else val

    def raw_or_default(self, index: int, default: int = 0) -> int:
        """Return parameter *index*, substituting *default* only when omitted."""
        if index >= len(self.values):
            return default
        if (self.omitted_mask >> index) & 1:
            return default
        return self.values[index]

    def is_subparam(self, index: int) -> bool:
        """Return ``True`` when parameter *index* was introduced by ``:``."""
        return bool((self.colon_mask >> index) & 1)

    def groups(self) -> list[tuple[int, ...]]:
        """Group ``values`` into semicolon-delimited parameter tuples."""
        if not self.values:
            return []
        result: list[list[int]] = [[self.values[0]]]
        for idx in range(1, len(self.values)):
            if (self.colon_mask >> idx) & 1:
                result[-1].append(self.values[idx])
            else:
                result.append([self.values[idx]])
        return [tuple(g) for g in result]


EMPTY_PARAMS = CsiParams()


@dataclass(frozen=True, slots=True)
class Print:
    """Decoded printable text emitted in ``GROUND`` state.

    Attributes
    ----------
    text : str
        Decoded Unicode string (one or more characters).
    """

    text: str


@dataclass(frozen=True, slots=True)
class Execute:
    """C0 control byte executed immediately.

    Attributes
    ----------
    byte : int
        Control code value (e.g. ``0x0A`` for ``LF``).
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
        Private marker byte in ``0x3C..0x3F`` (``<``, ``=``, ``>``, ``?``) or
        ``0`` when absent.
    params : CsiParams
        Parsed numeric parameters and colon/omission bitmasks.
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
    r"""Completed Operating System Command (``OSC``).

    Attributes
    ----------
    code : int
        Numeric ``OSC`` command identifier (``-1`` when absent or non-numeric).
    data : bytes
        Raw payload bytes following the first ``;`` (or the full body when no
        ``;`` is present).
    terminator : bytes
        Wire terminator that closed the command (``b"\x07"``, ``b"\x1b\\"``, or
        ``b"\x9c"`` in Latin-1 8-bit mode).
    """

    code: int
    data: bytes
    terminator: bytes = b"\x1b\\"


@dataclass(frozen=True, slots=True)
class DcsDispatch:
    """Completed Device Control String (``DCS``), including ``SIXEL``.

    Attributes
    ----------
    prefix : int
        Private marker byte in ``0x3C..0x3F`` or ``0``.
    params : CsiParams
        Header parameters preceding the payload.
    intermediates : bytes
        Header intermediate bytes in ``0x20..0x2F``.
    final : int
        Header final byte in ``0x40..0x7E``.
    data : bytes
        Raw passthrough payload bytes up to ``ST``.
    """

    prefix: int
    params: CsiParams
    intermediates: bytes
    final: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ApcDispatch:
    """Completed Application Program Command (``APC``), e.g. Kitty graphics.

    Attributes
    ----------
    data : bytes
        Raw ``APC`` payload bytes up to ``ST``.
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
    """Zero-allocation callback target for :meth:`Parser.feed_to`."""

    def on_print(self, text: str) -> None:
        """Receive printable Unicode text."""

    def on_execute(self, byte: int) -> None:
        """Receive a C0 control byte."""

    def on_esc(self, intermediates: bytes, final: int) -> None:
        """Receive an ``ESC`` sequence."""

    def on_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        """Receive a ``CSI`` sequence."""

    def on_osc(self, code: int, data: bytes, terminator: bytes) -> None:
        """Receive an ``OSC`` sequence and its closing terminator."""

    def on_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        """Receive a ``DCS`` sequence."""

    def on_apc(self, data: bytes) -> None:
        """Receive an ``APC`` sequence."""


class _ListSink:
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

    def on_osc(self, code: int, data: bytes, terminator: bytes) -> None:
        self.actions.append(OscDispatch(code, data, terminator))

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


def _parse_csi_param_bytes(raw: bytes, *, max_params: int) -> CsiParams:
    if not raw:
        return EMPTY_PARAMS
    vals: list[int] = []
    colon_mask = 0
    omitted_mask = 0
    cur = 0
    has_digit = False
    is_colon = False
    for b in raw:
        if 0x30 <= b <= 0x39:
            cur = min(_PARAM_CLAMP, cur * 10 + (b - 0x30))
            has_digit = True
        elif b in (0x3B, 0x3A):
            idx = len(vals)
            if idx < max_params:
                vals.append(cur)
                if is_colon:
                    colon_mask |= 1 << idx
                if not has_digit:
                    omitted_mask |= 1 << idx
            cur = 0
            has_digit = False
            is_colon = b == 0x3A
    idx = len(vals)
    if idx < max_params:
        vals.append(cur)
        if is_colon:
            colon_mask |= 1 << idx
        if not has_digit:
            omitted_mask |= 1 << idx
    return CsiParams(tuple(vals), colon_mask, omitted_mask)


def _decode_extended_color(
    sub: tuple[int, ...],
    flat_groups: list[tuple[int, ...]],
    group_idx: int,
) -> tuple[Color, int]:
    """Decode ``38`` / ``48`` / ``58`` color group into ``(color, consumed)``."""
    if len(sub) >= 2:
        mode = sub[1]
        if mode == 5 and len(sub) >= 3:
            cidx = sub[2]
            if 0 <= cidx <= 255:
                return IndexedColor(cidx), 1
            return None, 1
        if mode == 2:
            if len(sub) >= 6:
                r, g, b = sub[3], sub[4], sub[5]
            elif len(sub) == 5:
                r, g, b = sub[2], sub[3], sub[4]
            else:
                return None, 1
            if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
                return RgbColor(r, g, b), 1
            return None, 1
        return None, 1

    if group_idx + 1 >= len(flat_groups):
        return None, 1
    mode = flat_groups[group_idx + 1][0]
    if mode == 5 and group_idx + 2 < len(flat_groups):
        cidx = flat_groups[group_idx + 2][0]
        if 0 <= cidx <= 255:
            return IndexedColor(cidx), 3
        return None, 3
    if mode == 2 and group_idx + 4 < len(flat_groups):
        r = flat_groups[group_idx + 2][0]
        g = flat_groups[group_idx + 3][0]
        b = flat_groups[group_idx + 4][0]
        if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
            return RgbColor(r, g, b), 5
        return None, 5
    return None, 2


_UL_MAP: dict[int, UnderlineStyle] = {
    0: UnderlineStyle.NONE,
    1: UnderlineStyle.SINGLE,
    2: UnderlineStyle.DOUBLE,
    3: UnderlineStyle.CURLY,
    4: UnderlineStyle.DOTTED,
    5: UnderlineStyle.DASHED,
}


def parse_sgr(
    params: CsiParams,
    current: Style = DEFAULT_STYLE,
    *,
    memo: dict[tuple[Style, CsiParams], Style] | None = None,
) -> Style:
    """Apply ``CSI ... m`` parameters to *current* and return a new :class:`Style`."""
    if not params.values or (
        len(params.values) == 1 and params.values[0] == 0 and params.colon_mask == 0
    ):
        return DEFAULT_STYLE

    if memo is not None:
        cached = memo.get((current, params))
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
    total = len(groups)
    while idx < total:
        grp = groups[idx]
        code = grp[0]
        if code == 0 and len(grp) == 1:
            fg = None
            bg = None
            ul_color = None
            bold = False
            dim = False
            italic = False
            underline = UnderlineStyle.NONE
            blink = False
            reverse = False
            hidden = False
            strike = False
            overline = False
        elif code == 1:
            bold = True
        elif code == 2:
            dim = True
        elif code == 3:
            italic = True
        elif code == 4:
            sub_ul = grp[1] if len(grp) >= 2 else 1
            underline = _UL_MAP.get(sub_ul, UnderlineStyle.SINGLE)
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
            parsed_fg, step = _decode_extended_color(grp, groups, idx)
            if parsed_fg is not None:
                fg = parsed_fg
            idx += step
            continue
        elif code == 39:
            fg = None
        elif 40 <= code <= 47:
            bg = AnsiColor(code - 40)
        elif code == 48:
            parsed_bg, step = _decode_extended_color(grp, groups, idx)
            if parsed_bg is not None:
                bg = parsed_bg
            idx += step
            continue
        elif code == 49:
            bg = None
        elif code == 53:
            overline = True
        elif code == 55:
            overline = False
        elif code == 58:
            parsed_ul, step = _decode_extended_color(grp, groups, idx)
            if parsed_ul is not None:
                ul_color = parsed_ul
            idx += step
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
    if memo is not None:
        if len(memo) >= _MEMO_LIMIT:
            del memo[next(iter(memo))]
        memo[(current, params)] = result
    return result


class Parser:
    """Streaming VT500 / ANSI escape-sequence byte parser."""

    __slots__ = (
        "_csi_colon_mask",
        "_csi_cur",
        "_csi_has_digit",
        "_csi_is_colon",
        "_csi_memo",
        "_csi_omitted_mask",
        "_csi_prefix",
        "_csi_vals",
        "_dcs_final",
        "_dcs_params",
        "_encoding",
        "_intermediates",
        "_latin1",
        "_limits",
        "_state",
        "_str_buf",
        "_str_esc_origin",
        "_str_kind",
        "_str_overflow",
        "_tmux_dcs_state",
        "_tmux_passthrough",
        "_utf8",
        "_utf8_pending",
    )

    def __init__(
        self,
        *,
        encoding: ParserEncoding = "utf-8",
        tmux_passthrough: bool = True,
        limits: Limits = DEFAULT_LIMITS,
    ) -> None:
        if encoding not in ("utf-8", "latin-1"):
            msg = f"unsupported parser encoding: {encoding!r}"
            raise ValueError(msg)
        self._encoding: ParserEncoding = encoding
        self._latin1: bool = encoding == "latin-1"
        self._tmux_passthrough: bool = tmux_passthrough
        self._limits = limits
        self._state: ParserState = ParserState.GROUND
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._utf8_pending: bool = False
        self._intermediates = bytearray()
        self._csi_prefix: int = 0
        self._csi_vals: list[int] = []
        self._csi_colon_mask: int = 0
        self._csi_omitted_mask: int = 0
        self._csi_cur: int = 0
        self._csi_has_digit: bool = False
        self._csi_is_colon: bool = False
        self._dcs_params: CsiParams = EMPTY_PARAMS
        self._dcs_final: int = 0
        self._str_buf = bytearray()
        self._str_overflow: bool = False
        self._str_kind: int = 0
        self._str_esc_origin: ParserState | None = None
        self._tmux_dcs_state: int = 0
        self._csi_memo: dict[bytes, CsiParams] = {}

    @property
    def encoding(self) -> ParserEncoding:
        """Return wire character encoding (``"utf-8"`` or ``"latin-1"``)."""
        return self._encoding

    @property
    def state(self) -> ParserState:
        """Return the current :class:`ParserState`."""
        return self._state

    @property
    def utf8_pending(self) -> bool:
        """Return ``True`` when an incomplete multi-byte UTF-8 sequence is buffered."""
        return self._utf8_pending

    def reset(self) -> None:
        """Reset parser state and clear buffered partial sequences."""
        self._state = ParserState.GROUND
        self._utf8.reset()
        self._utf8_pending = False
        self._intermediates.clear()
        self._reset_csi()
        self._abort_string()

    def _reset_csi(self) -> None:
        self._csi_prefix = 0
        self._csi_vals.clear()
        self._csi_colon_mask = 0
        self._csi_omitted_mask = 0
        self._csi_cur = 0
        self._csi_has_digit = False
        self._csi_is_colon = False
        self._intermediates.clear()

    def _abort_string(self) -> None:
        self._str_buf.clear()
        self._str_overflow = False
        self._str_kind = 0
        self._str_esc_origin = None
        self._tmux_dcs_state = 0
        self._reset_csi()

    def _push_param(self, *, next_is_colon: bool) -> None:
        idx = len(self._csi_vals)
        if idx < self._limits.max_csi_params:
            self._csi_vals.append(self._csi_cur)
            if self._csi_is_colon:
                self._csi_colon_mask |= 1 << idx
            if not self._csi_has_digit:
                self._csi_omitted_mask |= 1 << idx
        self._csi_cur = 0
        self._csi_has_digit = False
        self._csi_is_colon = next_is_colon

    def _finalize_params(self) -> CsiParams:
        if not self._csi_vals and not self._csi_has_digit and not self._csi_is_colon:
            return EMPTY_PARAMS
        self._push_param(next_is_colon=False)
        return CsiParams(
            values=tuple(self._csi_vals),
            colon_mask=self._csi_colon_mask,
            omitted_mask=self._csi_omitted_mask,
        )

    def _flush_incomplete_utf8(self, sink: ActionSink) -> None:
        if self._utf8_pending:
            flushed = self._utf8.decode(b"", final=True)
            self._utf8.reset()
            self._utf8_pending = False
            if flushed:
                sink.on_print(flushed)

    def _decode_utf8_bytes(self, raw: bytes | memoryview, sink: ActionSink) -> None:
        chunk = bytes(raw) if isinstance(raw, memoryview) else raw
        decoded = self._utf8.decode(chunk, final=False)
        self._utf8_pending = bool(self._utf8.getstate()[0])
        if decoded:
            cleaned = _C1_CHAR_RE.sub("", decoded)
            if cleaned:
                sink.on_print(cleaned)

    def _dispatch_osc(self, sink: ActionSink, terminator: bytes) -> None:
        if self._str_overflow:
            self._abort_string()
            return
        raw = bytes(self._str_buf)
        self._abort_string()
        semi = raw.find(b";")
        if semi == -1:
            code = int(raw) if raw.isdigit() else -1
            sink.on_osc(code, b"", terminator)
        else:
            head = raw[:semi]
            code = int(head) if head.isdigit() else -1
            sink.on_osc(code, raw[semi + 1 :], terminator)

    def _dispatch_dcs(self, sink: ActionSink) -> None:
        if not self._str_overflow:
            sink.on_dcs(
                self._csi_prefix,
                self._dcs_params,
                bytes(self._intermediates),
                self._dcs_final,
                bytes(self._str_buf),
            )
        self._abort_string()

    def _dispatch_apc(self, sink: ActionSink) -> None:
        if self._str_kind == 0x5F and not self._str_overflow:
            sink.on_apc(bytes(self._str_buf))
        self._abort_string()

    def _enter_dcs_passthrough(self, final_byte: int) -> None:
        self._dcs_params = self._finalize_params()
        self._dcs_final = final_byte
        self._str_buf.clear()
        self._str_overflow = False
        self._tmux_dcs_state = (
            1
            if (
                self._tmux_passthrough
                and self._csi_prefix == 0
                and not self._intermediates
                and not self._dcs_params.values
                and final_byte == 0x74
            )
            else 0
        )
        self._state = ParserState.DCS_PASSTHROUGH

    def _handle_c1_anywhere(self, b: int, sink: ActionSink) -> None:
        self._abort_string()
        if b == 0x9B:  # CSI
            self._state = ParserState.CSI_ENTRY
        elif b == 0x9D:  # OSC
            self._state = ParserState.OSC_STRING
        elif b == 0x90:  # DCS
            self._state = ParserState.DCS_ENTRY
        elif b in (0x98, 0x9E, 0x9F):  # SOS, PM, APC
            self._str_kind = b - 0x40
            self._state = ParserState.SOS_PM_APC_STRING
        elif b == 0x9C:  # ST
            self._state = ParserState.GROUND
        else:
            self._state = ParserState.GROUND
            sink.on_esc(b"", b - 0x40)

    def _cached_csi_params(self, pbytes: bytes) -> CsiParams:
        if not pbytes:
            return EMPTY_PARAMS
        cached = self._csi_memo.get(pbytes)
        if cached is not None:
            return cached
        parsed = _parse_csi_param_bytes(
            pbytes,
            max_params=self._limits.max_csi_params,
        )
        if len(self._csi_memo) >= _MEMO_LIMIT:
            del self._csi_memo[next(iter(self._csi_memo))]
        self._csi_memo[pbytes] = parsed
        return parsed

    def feed(self, data: collections.abc.Buffer) -> list[Action]:
        """Feed *data* and return all completed :data:`Action` items."""
        sink = _ListSink()
        self.feed_to(data, sink)
        return sink.actions

    def feed_to(self, data: collections.abc.Buffer, sink: ActionSink) -> None:
        """Feed *data* directly into *sink* without allocating action wrappers."""
        mv = memoryview(data).cast("B")
        n = len(mv)
        pos = 0
        latin1 = self._latin1
        max_osc = self._limits.max_osc_bytes
        max_dcs = self._limits.max_dcs_bytes
        max_apc = self._limits.max_apc_bytes

        while pos < n:
            state = self._state

            if state == ParserState.GROUND:
                if latin1:
                    end = pos
                    while end < n and (0x20 <= mv[end] <= 0x7E or mv[end] >= 0xA0):
                        end += 1
                    if end > pos:
                        sink.on_print(bytes(mv[pos:end]).decode("latin-1"))
                        pos = end
                        if pos >= n:
                            break
                    b_lat = mv[pos]
                    if 0x80 <= b_lat <= 0x9F:
                        pos += 1
                        self._handle_c1_anywhere(b_lat, sink)
                        continue
                else:
                    match = _CTRL_RE.search(mv, pos)
                    end = match.start() if match is not None else n
                    if end > pos:
                        span = mv[pos:end]
                        if not self._utf8_pending:
                            raw_span = bytes(span)
                            if raw_span.isascii():
                                sink.on_print(raw_span.decode("ascii"))
                                pos = end
                                if pos >= n:
                                    break
                            else:
                                self._decode_utf8_bytes(raw_span, sink)
                                pos = end
                                if pos >= n:
                                    break
                        else:
                            self._decode_utf8_bytes(span, sink)
                            pos = end
                            if pos >= n:
                                break

                b = mv[pos]
                pos += 1
                if b == 0x1B:
                    self._flush_incomplete_utf8(sink)
                    if pos < n and mv[pos] == 0x5B:  # '[' -> CSI fast path
                        csi_m = _CSI_FAST_RE.match(mv, pos + 1)
                        if csi_m is not None:
                            pfx_b, p_mv, fin_b = csi_m.groups()
                            pfx = pfx_b[0] if pfx_b else 0
                            params = self._cached_csi_params(bytes(p_mv))
                            sink.on_csi(pfx, params, b"", fin_b[0])
                            pos = csi_m.end()
                            continue
                    self._intermediates.clear()
                    self._state = ParserState.ESCAPE
                elif b == 0x7F:
                    pass
                else:
                    self._flush_incomplete_utf8(sink)
                    sink.on_execute(b)
                continue

            if state == ParserState.OSC_STRING:
                start = pos
                while pos < n:
                    b_scan = mv[pos]
                    if b_scan in (0x07, 0x18, 0x1A, 0x1B) or (
                        latin1 and 0x80 <= b_scan <= 0x9F
                    ):
                        break
                    pos += 1
                if pos > start and not self._str_overflow:
                    chunk_len = pos - start
                    if len(self._str_buf) + chunk_len <= max_osc:
                        self._str_buf.extend(mv[start:pos])
                    else:
                        self._str_buf.clear()
                        self._str_overflow = True
                if pos >= n:
                    break
                b = mv[pos]
                pos += 1
                if b == 0x07:
                    self._dispatch_osc(sink, b"\x07")
                    self._state = ParserState.GROUND
                elif latin1 and b == 0x9C:
                    self._dispatch_osc(sink, b"\x9c")
                    self._state = ParserState.GROUND
                elif latin1 and 0x80 <= b <= 0x9F:
                    self._handle_c1_anywhere(b, sink)
                elif b == 0x1B:
                    self._str_esc_origin = ParserState.OSC_STRING
                    self._state = ParserState.STRING_ESC
                else:
                    self._abort_string()
                    self._state = ParserState.GROUND
                continue

            if state == ParserState.DCS_PASSTHROUGH:
                start = pos
                while pos < n:
                    b_scan = mv[pos]
                    if b_scan in (0x18, 0x1A, 0x1B, 0x7F) or (
                        latin1 and 0x80 <= b_scan <= 0x9F
                    ):
                        break
                    pos += 1
                if pos > start and not self._str_overflow:
                    chunk_len = pos - start
                    if len(self._str_buf) + chunk_len <= max_dcs:
                        self._str_buf.extend(mv[start:pos])
                        if self._tmux_dcs_state == 1 and len(self._str_buf) >= 4:
                            self._tmux_dcs_state = (
                                2 if self._str_buf[:4] == b"mux;" else -1
                            )
                    else:
                        self._str_buf.clear()
                        self._str_overflow = True
                if pos >= n:
                    break
                b = mv[pos]
                pos += 1
                if b == 0x7F:
                    continue
                if latin1 and b == 0x9C:
                    self._dispatch_dcs(sink)
                    self._state = ParserState.GROUND
                elif latin1 and 0x80 <= b <= 0x9F:
                    self._handle_c1_anywhere(b, sink)
                elif b == 0x1B:
                    self._str_esc_origin = ParserState.DCS_PASSTHROUGH
                    self._state = ParserState.STRING_ESC
                else:
                    self._abort_string()
                    self._state = ParserState.GROUND
                continue

            if state == ParserState.SOS_PM_APC_STRING:
                start = pos
                while pos < n:
                    b_scan = mv[pos]
                    if b_scan in (0x18, 0x1A, 0x1B) or (
                        latin1 and 0x80 <= b_scan <= 0x9F
                    ):
                        break
                    pos += 1
                if pos > start and self._str_kind == 0x5F and not self._str_overflow:
                    chunk_len = pos - start
                    if len(self._str_buf) + chunk_len <= max_apc:
                        self._str_buf.extend(mv[start:pos])
                    else:
                        self._str_buf.clear()
                        self._str_overflow = True
                if pos >= n:
                    break
                b = mv[pos]
                pos += 1
                if latin1 and b == 0x9C:
                    self._dispatch_apc(sink)
                    self._state = ParserState.GROUND
                elif latin1 and 0x80 <= b <= 0x9F:
                    self._handle_c1_anywhere(b, sink)
                elif b == 0x1B:
                    self._str_esc_origin = ParserState.SOS_PM_APC_STRING
                    self._state = ParserState.STRING_ESC
                else:
                    self._abort_string()
                    self._state = ParserState.GROUND
                continue

            if state == ParserState.STRING_ESC:
                b = mv[pos]
                pos += 1
                origin = self._str_esc_origin
                if b == 0x5C:  # 'ESC \' -> 7-bit ST
                    if origin == ParserState.OSC_STRING:
                        self._dispatch_osc(sink, b"\x1b\\")
                    elif origin == ParserState.DCS_PASSTHROUGH:
                        self._dispatch_dcs(sink)
                    elif origin == ParserState.SOS_PM_APC_STRING:
                        self._dispatch_apc(sink)
                    else:
                        self._abort_string()
                    self._state = ParserState.GROUND
                    continue
                if (
                    b == 0x1B
                    and origin == ParserState.DCS_PASSTHROUGH
                    and self._tmux_dcs_state == 2
                ):
                    if not self._str_overflow:
                        if len(self._str_buf) + 1 <= max_dcs:
                            self._str_buf.append(0x1B)
                        else:
                            self._str_buf.clear()
                            self._str_overflow = True
                    self._str_esc_origin = None
                    self._state = ParserState.DCS_PASSTHROUGH
                    continue
                # Any other byte aborts the unclosed string per DEC STD 070 §5.1.2.3
                # and re-evaluates b starting from ESCAPE.
                self._abort_string()
                self._state = ParserState.ESCAPE
                pos -= 1
                continue

            b = mv[pos]
            pos += 1

            if latin1 and 0x80 <= b <= 0x9F:
                self._handle_c1_anywhere(b, sink)
                continue

            if b == 0x1B:
                self._abort_string()
                self._state = ParserState.ESCAPE
                continue

            if b in (0x18, 0x1A):
                self._abort_string()
                self._state = ParserState.GROUND
                continue

            if state == ParserState.DCS_IGNORE:
                continue

            if b < 0x20:
                sink.on_execute(b)
                continue
            if b == 0x7F:
                continue

            if state == ParserState.ESCAPE:
                if 0x20 <= b <= 0x2F:
                    self._intermediates.append(b)
                    self._state = ParserState.ESCAPE_INTERMEDIATE
                elif b == 0x5B:  # '[' -> CSI
                    self._reset_csi()
                    self._state = ParserState.CSI_ENTRY
                elif b == 0x5D:  # ']' -> OSC
                    self._str_buf.clear()
                    self._str_overflow = False
                    self._state = ParserState.OSC_STRING
                elif b == 0x50:  # 'P' -> DCS
                    self._reset_csi()
                    self._str_buf.clear()
                    self._str_overflow = False
                    self._state = ParserState.DCS_ENTRY
                elif b in (0x58, 0x5E, 0x5F):  # SOS, PM, APC
                    self._str_buf.clear()
                    self._str_overflow = False
                    self._str_kind = b
                    self._state = ParserState.SOS_PM_APC_STRING
                elif 0x30 <= b <= 0x7E:
                    sink.on_esc(b"", b)
                    self._state = ParserState.GROUND
                else:
                    self._state = ParserState.GROUND

            elif state == ParserState.ESCAPE_INTERMEDIATE:
                if 0x20 <= b <= 0x2F:
                    if len(self._intermediates) < MAX_INTERMEDIATES:
                        self._intermediates.append(b)
                elif 0x30 <= b <= 0x7E:
                    sink.on_esc(bytes(self._intermediates), b)
                    self._intermediates.clear()
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
                elif b == 0x3B:
                    self._push_param(next_is_colon=False)
                    self._state = ParserState.CSI_PARAM
                elif b == 0x3A:
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
                    self._reset_csi()
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
                    self._enter_dcs_passthrough(b)
                else:
                    self._state = ParserState.DCS_IGNORE

            elif state == ParserState.DCS_INTERMEDIATE:
                if 0x20 <= b <= 0x2F:
                    if len(self._intermediates) < MAX_INTERMEDIATES:
                        self._intermediates.append(b)
                elif 0x40 <= b <= 0x7E:
                    self._enter_dcs_passthrough(b)
                else:
                    self._state = ParserState.DCS_IGNORE


__all__ = [
    "EMPTY_PARAMS",
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
    "ParserEncoding",
    "ParserState",
    "Print",
    "parse_sgr",
]
