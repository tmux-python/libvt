(adr-sequence-codec-and-explainer)=

# ADR 0009: Cold-path sequence disassembler and `python -m libvt` CLI

## Status

Accepted. 2026-11-21.

## Context

When debugging a failing `tmux`, TUI, or shell test, raw byte dumps such as
`b"\x1b[?2026h\x1b[38:2::40:120:200m\x1b]7501;state=working\x1b\\"` are tedious
to decode by hand. Outsider architecture reviews highlighted the value of a
declarative sequence explainer (`libvt.explain(data)` and `python -m libvt
explain`), while performance profiling showed that routing `Terminal.feed()`'s
hot loop through boxed `Token` -> `Registry` -> `Command` objects degrades
throughput by `4-8x`.

## Decision

Keep hot-path emulation and cold-path sequence disassembly cleanly separated:

1. **Cold-path `libvt.codec` (`explain` and `format_explain`)**:
   - `explain(data: Buffer | str) -> tuple[ExplainedSpan, ...]` drives `Parser`
     with a cold-path `_ExplainSink` and maps each action (`Print`, `Execute`,
     `EscDispatch`, `CsiDispatch`, `OscDispatch`, `DcsDispatch`, `ApcDispatch`)
     through compact lookup tables to produce frozen `ExplainedSpan(kind,
     mnemonic, summary, raw)` records (e.g. `CSI`, `"DECSET"`,
     `"?2026 (SYNCHRONIZED_OUTPUT) = set"`).
   - `format_explain(data: Buffer | str) -> str` formats one human-readable
     line per span for terminal debugging and pytest failure reports.
2. **Zero-dependency CLI (`src/libvt/__main__.py`)**:
   - `python -m libvt explain` reads stdin (or a file path) and prints
     `format_explain` output.
   - `python -m libvt render [--rows 24] [--cols 80]` renders final plain-text
     screen state.
   - `python -m libvt strip` strips VT escape sequences from stdin/file.
3. **Hot path untouched**:
   - `Terminal.feed()` never imports `libvt.codec` and never instantiates
     `ExplainedSpan` objects.

## Alternatives considered

- **Unified hot/cold `@csi` `Command` registry**: Rejected because allocating a
  `Command` dataclass per `SGR` or `CUP` sequence penalizes every `Terminal.feed`
  call for metadata only humans read during debugging.
- **Registering a global `vt` console-script binary in `[project.scripts]`**:
  Omitted to avoid colliding with external `vt` binaries on user `PATH`;
  `python -m libvt` is zero-collision and always matches the active virtualenv.

## Consequences

### Positive

- Developers and pytest failure hooks (`FlightRecorder`) disassemble arbitrary
  VT traces with `libvt.explain()` or `python -m libvt explain` with zero
  impact on `Terminal.feed()` speed.

### Tradeoffs

- Sequence mnemonics in `libvt.codec` are maintained alongside `Terminal`'s
  dispatch handlers, tested for parity in `tests/test_codec.py`.

### Risks

- Raw untrusted bytes inside `format_explain` emitting live escape sequences to
  the developer's own terminal. Mitigation: `format_explain` escapes all C0,
  C1, `ESC`, and BiDi override characters in both `raw` and `summary` columns.
