"""Unit tests for the 14-state VT500 streaming parser and SGR decoder."""

from __future__ import annotations

from libvt.parser import (
    MAX_CSI_PARAMS,
    MAX_OSC_BYTES,
    ApcDispatch,
    CsiDispatch,
    CsiParams,
    DcsDispatch,
    Execute,
    OscDispatch,
    Parser,
    ParserState,
    Print,
    parse_sgr,
)
from libvt.style import (
    DEFAULT_STYLE,
    AnsiColor,
    IndexedColor,
    RgbColor,
    UnderlineStyle,
)


def test_parser_utf8_and_csi_across_single_byte_chunks() -> None:
    """Incomplete UTF-8 codepoints and CSI sequences survive 1-byte feeds."""
    raw = b"\x1b[38:2::10:20:30;4:3m\xe6\x97\xa5\xe6\x9c\xac\x1b[0m"
    one_shot = Parser().feed(raw)

    chunked_parser = Parser()
    chunked = [act for b in raw for act in chunked_parser.feed(bytes([b]))]

    assert chunked_parser.state == ParserState.GROUND
    assert len(one_shot) == 3
    assert chunked == [
        one_shot[0],
        Print("日"),
        Print("本"),
        one_shot[2],
    ]


def test_c0_executes_inside_csi_without_aborting_sequence() -> None:
    """ECMA-48 C0 controls inside ``CSI_PARAM`` execute and resume parsing."""
    actions = Parser().feed(b"\x1b[1\r;32m")
    assert actions == [
        Execute(0x0D),
        CsiDispatch(
            prefix=0,
            params=CsiParams(values=(1, 32), colon_mask=0, omitted_mask=0),
            intermediates=b"",
            final=ord("m"),
        ),
    ]


def test_parse_sgr_colon_and_semicolon_forms() -> None:
    """Colon ITU T.416 and legacy semicolon SGR forms decode identically."""
    p_colon = Parser().feed(b"\x1b[1;4:3;38:2::12:34:56;48:5:208;58:2:1:2:3m")
    assert isinstance(p_colon[0], CsiDispatch)
    style = parse_sgr(p_colon[0].params, DEFAULT_STYLE)
    assert style.bold is True
    assert style.underline == UnderlineStyle.CURLY
    assert style.fg == RgbColor(12, 34, 56)
    assert style.bg == IndexedColor(208)
    assert style.underline_color == RgbColor(1, 2, 3)

    p_semi = Parser().feed(b"\x1b[31;102m")
    assert isinstance(p_semi[0], CsiDispatch)
    style2 = parse_sgr(p_semi[0].params, style)
    assert style2.fg == AnsiColor.RED
    assert style2.bg == AnsiColor.BRIGHT_GREEN
    assert parse_sgr(CsiParams(), style2) == DEFAULT_STYLE


def test_parser_bounds_overflow_and_recovers_to_ground() -> None:
    """Oversized CSI parameters and OSC strings do not prevent ground recovery."""
    parser = Parser()
    many_params = ";".join("1" for _ in range(MAX_CSI_PARAMS + 20)).encode()
    actions = parser.feed(b"\x1b[" + many_params + b"mOK")
    assert isinstance(actions[0], CsiDispatch)
    assert len(actions[0].params.values) == MAX_CSI_PARAMS
    assert actions[1] == Print("OK")

    huge_osc = b"\x1b]0;" + (b"x" * (MAX_OSC_BYTES + 100)) + b"\x07AFTER"
    osc_actions = parser.feed(huge_osc)
    assert not any(isinstance(a, OscDispatch) for a in osc_actions)
    assert osc_actions == [Print("AFTER")]


def test_parser_dcs_sixel_and_apc_kitty() -> None:
    r"""``DCS`` and ``APC`` payloads terminate cleanly on 7-bit ``ESC \\``."""
    actions = Parser().feed(b"\x1bP0;1q#0;2;0;0;0~\x1b\\\x1b_Ga=T,f=32;AAAA\x1b\\")
    assert len(actions) == 2
    assert isinstance(actions[0], DcsDispatch)
    assert actions[0].final == ord("q")
    assert actions[0].data == b"#0;2;0;0;0~"
    assert isinstance(actions[1], ApcDispatch)
    assert actions[1].data == b"Ga=T,f=32;AAAA"
