"""Cold-path VT byte-stream disassembler for ``libvt.explain()`` and failure dumps."""

from __future__ import annotations

import collections.abc
from dataclasses import dataclass

from libvt._ucd import sanitize_terminal_text
from libvt.modes import DEC_PRIVATE_MASK, Mode
from libvt.parser import (
    ApcDispatch,
    CsiDispatch,
    DcsDispatch,
    EscDispatch,
    Execute,
    OscDispatch,
    Parser,
    Print,
)


@dataclass(frozen=True, slots=True)
class ExplainedSpan:
    """Human-readable disassembly of one parsed VT token.

    Attributes
    ----------
    kind : str
        Token family (``"PRINT"``, ``"C0"``, ``"ESC"``, ``"CSI"``, ``"OSC"``,
        ``"DCS"``, or ``"APC"``).
    mnemonic : str
        Standard ECMA-48 / DEC / modern VT mnemonic (e.g. ``"CUP"``, ``"SGR"``,
        ``"DECSET"``, ``"OSC 7501"``).
    summary : str
        Sanitized parameter and meaning summary.
    """

    kind: str
    mnemonic: str
    summary: str


_C0_NAMES: dict[int, tuple[str, str]] = {
    0x07: ("BEL", "bell"),
    0x08: ("BS", "backspace"),
    0x09: ("HT", "horizontal tab"),
    0x0A: ("LF", "line feed"),
    0x0B: ("VT", "vertical tab"),
    0x0C: ("FF", "form feed"),
    0x0D: ("CR", "carriage return"),
    0x0E: ("SO", "shift out -> G1"),
    0x0F: ("SI", "shift in -> G0"),
}

_CSI_NAMES: dict[str, str] = {
    "@": "ICH",
    "A": "CUU",
    "B": "CUD",
    "C": "CUF",
    "D": "CUB",
    "E": "CNL",
    "F": "CPL",
    "G": "CHA",
    "H": "CUP",
    "I": "CHT",
    "J": "ED",
    "K": "EL",
    "L": "IL",
    "M": "DL",
    "P": "DCH",
    "S": "SU",
    "T": "SD",
    "X": "ECH",
    "Z": "CBT",
    "`": "HPA",
    "a": "HPR",
    "b": "REP",
    "c": "DA1",
    "d": "VPA",
    "e": "VPR",
    "f": "HVP",
    "g": "TBC",
    "h": "SM",
    "l": "RM",
    "m": "SGR",
    "n": "DSR",
    "q": "XTVERSION/DECSCUSR",
    "r": "DECSTBM",
    "s": "DECSLRM/SCOSC",
    "t": "XTWINOPS",
    "u": "SCORC/KITTY_KBD",
}

_OSC_NAMES: dict[int, str] = {
    0: "OSC 0 (icon+title)",
    1: "OSC 1 (icon)",
    2: "OSC 2 (title)",
    4: "OSC 4 (palette)",
    7: "OSC 7 (cwd)",
    8: "OSC 8 (hyperlink)",
    9: "OSC 9 (notification/progress)",
    10: "OSC 10 (fg color)",
    11: "OSC 11 (bg color)",
    12: "OSC 12 (cursor color)",
    22: "OSC 22 (pointer shape)",
    52: "OSC 52 (clipboard)",
    66: "OSC 66 (text sizing)",
    99: "OSC 99 (kitty notification)",
    104: "OSC 104 (reset palette)",
    110: "OSC 110 (reset fg color)",
    111: "OSC 111 (reset bg color)",
    112: "OSC 112 (reset cursor color)",
    133: "OSC 133 (semantic prompt)",
    777: "OSC 777 (notification)",
    3008: "OSC 3008 (context)",
    5522: "OSC 5522 (mime clipboard)",
    7501: "OSC 7501 (program status)",
}


def _describe_mode(code: int, *, private: bool) -> str:
    prefix = "?" if private else ""
    if not (0 <= code < DEC_PRIVATE_MASK):
        return f"{prefix}{code}"
    raw = (DEC_PRIVATE_MASK | code) if private else code
    try:
        name = Mode(raw).name
    except ValueError:
        return f"{prefix}{code}"
    return f"{prefix}{code} ({name})"


def _explain_csi(act: CsiDispatch) -> ExplainedSpan:
    pfx = chr(act.prefix) if act.prefix else ""
    inter = act.intermediates.decode("ascii", errors="replace")
    fin = chr(act.final)
    vals = ";".join(str(v) for v in act.params.values)

    if act.prefix == ord("?") and not inter and fin in ("h", "l"):
        mnem = "DECSET" if fin == "h" else "DECRST"
        modes = ", ".join(_describe_mode(v, private=True) for v in act.params.values)
        state_word = "set" if fin == "h" else "reset"
        return ExplainedSpan("CSI", mnem, f"{modes} = {state_word}")

    if inter == "$" and fin == "p":
        code = act.params.raw_or_default(0, 0)
        return ExplainedSpan(
            "CSI",
            "DECRQM",
            _describe_mode(code, private=act.prefix == ord("?")),
        )

    if fin == "u" and pfx in (">", "<", "=", "?"):
        return ExplainedSpan("CSI", "KITTY_KBD", f"{pfx}{vals}u")

    if pfx == ">" and fin == "q" and not inter:
        return ExplainedSpan("CSI", "XTVERSION", ">q")

    if inter == '"' and fin == "q":
        return ExplainedSpan("CSI", "DECSCA", f'{vals}"q')

    if pfx == "?" and fin in ("J", "K"):
        mnem = "DECSED" if fin == "J" else "DECSEL"
        return ExplainedSpan("CSI", mnem, f"?{vals}{fin}")

    mnem = _CSI_NAMES.get(fin, f"CSI {fin}")
    return ExplainedSpan("CSI", mnem, f"{pfx}{vals}{inter}{fin}")


def explain(data: collections.abc.Buffer | str) -> tuple[ExplainedSpan, ...]:
    """Disassemble *data* into sanitized :class:`ExplainedSpan` records."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    parser = Parser()
    actions = parser.feed(raw)
    spans: list[ExplainedSpan] = []

    for act in actions:
        if isinstance(act, Print):
            spans.append(
                ExplainedSpan("PRINT", "TEXT", repr(sanitize_terminal_text(act.text))),
            )
        elif isinstance(act, Execute):
            mnem, desc = _C0_NAMES.get(act.byte, (f"0x{act.byte:02X}", "control"))
            spans.append(ExplainedSpan("C0", mnem, f"{desc} (0x{act.byte:02x})"))
        elif isinstance(act, EscDispatch):
            inter = act.intermediates.decode("ascii", errors="replace")
            fin = chr(act.final)
            if inter in ("(", ")", "*", "+"):
                slot = {"(": "G0", ")": "G1", "*": "G2", "+": "G3"}[inter]
                spans.append(ExplainedSpan("ESC", "SCS", f"{slot} -> {fin!r}"))
            else:
                spans.append(ExplainedSpan("ESC", f"ESC {inter}{fin}", f"{inter}{fin}"))
        elif isinstance(act, CsiDispatch):
            spans.append(_explain_csi(act))
        elif isinstance(act, OscDispatch):
            mnem = _OSC_NAMES.get(act.code, f"OSC {act.code}")
            body = sanitize_terminal_text(act.data.decode("utf-8", errors="replace"))
            if len(body) > 96:
                body = body[:96] + "..."
            spans.append(ExplainedSpan("OSC", mnem, body))
        elif isinstance(act, DcsDispatch):
            inter = act.intermediates.decode("ascii", errors="replace")
            fin = chr(act.final)
            if not act.prefix and not inter and fin == "q":
                mnem = "SIXEL"
            elif inter == "$":
                mnem = "DECRQSS"
            elif inter == "+":
                mnem = "XTGETTCAP"
            else:
                mnem = f"DCS {inter}{fin}"
            preview = sanitize_terminal_text(
                act.data[:64].decode("utf-8", errors="replace"),
            )
            spans.append(
                ExplainedSpan("DCS", mnem, f"len={len(act.data)} {preview!r}"),
            )
        elif isinstance(act, ApcDispatch):
            mnem = "KITTY_GFX" if act.data.startswith(b"G") else "APC"
            preview = sanitize_terminal_text(
                act.data[:64].decode("utf-8", errors="replace"),
            )
            spans.append(
                ExplainedSpan("APC", mnem, f"len={len(act.data)} {preview!r}"),
            )

    return tuple(spans)


def format_explain(data: collections.abc.Buffer | str) -> str:
    """Format :func:`explain` spans as aligned one-line records."""
    return "\n".join(
        f"{sp.kind:<5} {sp.mnemonic:<28} {sp.summary}" for sp in explain(data)
    )


__all__ = [
    "ExplainedSpan",
    "explain",
    "format_explain",
]
