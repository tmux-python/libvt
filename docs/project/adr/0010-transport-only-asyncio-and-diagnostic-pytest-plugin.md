(adr-asyncio-and-pytest-plugin)=

# ADR 0010: Transport-only asyncio/PTY adapters and diagnostic pytest plugin

## Status

Accepted. 2026-11-21.

## Context

`libvt` ships an `asyncio` / POSIX PTY transport layer (`libvt.aio`), visual
assertion helpers (`libvt.testing`), and a `pytest11` plugin
(`libvt.pytest_plugin`). Four concrete failure modes emerged during review and
empirical testing:

1. **Import pollution**: Importing `asyncio`, `pty`, or `pytest` inside
   `libvt/__init__.py` slows every `import libtmux` call.
2. **Fixture namespace collisions**: Auto-discovered `pytest11` plugins that
   register unprefixed fixture names (`terminal`, `spawn`, `shell`) collide
   with fixtures in `pytest-shell-utilities`, `pexpect`, and downstream test
   suites, whereas splitting a separate `pytest-vt` PyPI package creates
   cross-package version skew relative to `libtmux.pytest_plugin`.
3. **Wide-glyph box misalignment and terminal-escape injection in failure
   diffs**: Padding `format_screen_box` rows with `str.ljust(w)` counts Python
   codepoints instead of terminal display columns, pushing right `│` borders
   out of alignment on every 2-column CJK/emoji character (`"日本"`), while
   unescaped C0/C1/BiDi characters in failure messages can corrupt the test
   runner's terminal.
4. **POSIX vs Windows PTY scope**: Both `libtmux` and `tmuxp` declare
   `Operating System :: POSIX` and `Operating System :: MacOS :: MacOS X`
   classifiers only (`tmux` itself is POSIX-only). Adding a `pywinpty` C/Rust
   ConPTY dependency for Windows subprocess PTYs would break ADR 0001's pure
   `py3-none-any.whl` zero-dependency guarantee.

## Decision

1. **Strict import isolation (`libvt/__init__.py`)**:
   - `import libvt` never loads `asyncio`, `pty`, `termios`, or `pytest`
     (enforced by a clean-interpreter subprocess test in `tests/test_libvt.py`).
2. **`AsyncTerminalStream` and POSIX `open_pty()` (`libvt.aio`)**:
   - `AsyncTerminalStream` drives `Terminal` from `(StreamReader, StreamWriter)`
     pairs, flushing `drain_replies()` automatically and enforcing
     `sync_timeout` via `Terminal.expire_synchronized_output(sync_gen)` in both
     background-task (`async with`) and on-demand `wait_for()` modes.
   - `open_pty(argv, *, rows=24, cols=80, env=None, cwd=None)` spawns a child
     process attached to a stdlib `pty.openpty()` master/slave pair on POSIX,
     sets `TIOCSWINSZ`, pumps output + `drain_replies()`, and exposes
     `send()`, `send_key()`, `paste()`, `resize()`, and `wait_for()`. On
     Windows (`sys.platform == "win32"`), `open_pty()` raises
     `NotImplementedError` cleanly while the entire Sans-I/O core works
     unchanged.
3. **Strictly `vt_`-prefixed, `Protocol`-typed `pytest11` plugin
   (`libvt.pytest_plugin`)**:
   - Register `vt_terminal`, `vt_factory` (typed via `TerminalFactory`
     `t.Protocol` so static type checkers verify keyword arguments),
     `vt_parser`, `vt_recorder` (`FlightRecorder`), and `vt_pty` (calling
     `pytest.skip("POSIX pty required")` on `win32`), with lazy imports inside
     fixture bodies.
4. **Column-exact, sanitized `assert_screen` and `FlightRecorder`
   (`libvt.testing`)**:
   - `format_screen_box` and `assert_screen` measure cells by terminal display
     width (`WidthRole` / `display_width`), sanitize C0/C1 and BiDi overrides,
     display `W` soft-wrap markers, and append `FlightRecorder` diagnostics
     (cursor, active modes, `OSC 7501` status, `Parser.state`, and disassembled
     recent raw bytes via `format_explain`) when a `FlightRecorder` is attached
     to `Terminal`.
   - `Snapshot.to_dict()` returns a deterministic JSON-compatible dictionary
     for snapshot testing (`inline-snapshot` / `syrupy`).

## Alternatives considered

- **Unprefixed `terminal`, `spawn`, `shell` fixtures**: Rejected due to global
  `pytest11` namespace collisions.
- **Separate `pytest-vt` PyPI package**: Rejected to match `libtmux.pytest_plugin`
  single-package ergonomics.
- **Optional `pywinpty` dependency**: Deferred; `libtmux` and `tmuxp` target
  POSIX/macOS exclusively, and keeping `dependencies = []` preserves universal
  pure-Python wheels.

## Consequences

### Positive

- `assert_screen` aligns CJK, emoji, and combining marks to exact terminal
  columns and never leaks raw escape or BiDi spoofing sequences into pytest
  logs.
- Failed async or PTY tests include the exact disassembled tail of VT bytes
  that produced the mismatch.

### Tradeoffs

- Live subprocess `vt_pty` tests run on Linux/macOS/BSD and skip on native
  Windows, whereas all in-memory `vt_terminal` / `AsyncTerminalStream` tests
  run on every OS.

### Risks

- Leaked PTY file descriptors or child processes if a test fails mid-wait.
  Mitigation: `PtySession.__aexit__` closes the master fd, terminates/reaps the
  child process, and cancels the pump task inside `try / finally`.
