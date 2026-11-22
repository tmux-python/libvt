(adr-asyncio-and-pytest-plugin)=

# ADR 0005: Transport-only asyncio adapter and namespaced pytest plugin

## Status

Accepted. 2026-11-21.

## Context

`libvt` ships a `pytest11` plugin entry point (`libvt = "libvt.pytest_plugin"`)
and an `asyncio` stream wrapper for live PTY and socket sessions. Two failure
modes appear in Python libraries that bundle both:

1. Importing `asyncio` or `pytest` inside `libvt/__init__.py` adds startup
   cost to every consumer (`import libtmux`) even when neither subsystem is
   used.
2. Auto-discovered `pytest11` plugins that register generic fixture names
   (`terminal`, `screen`, `parser`) collide with fixtures in downstream
   repositories, or eagerly import the entire library during `pytest`
   collection even on test runs that never request a VT fixture.

## Decision

1. **Zero optional-subsystem imports on `import libvt`**:
   - `libvt/__init__.py` never imports `asyncio` or `pytest` (verified in CI
     via a fresh-interpreter subprocess test).
   - `libvt.aio` is imported explicitly (`from libvt.aio import
     AsyncTerminalStream`) and wraps any `(StreamReader, StreamWriter)` pair
     with `asyncio.Event` frame notifications, `?2026` hold coalescing with
     `sync_timeout: float = 1.0`, `wait_for(predicate, *, timeout=...)`, and
     automatic `drain_replies()` flushing back to the writer.
2. **Import-light, `vt_`-prefixed `pytest11` plugin (`libvt.pytest_plugin`)**:
   - Every fixture uses the `vt_` prefix: `vt_terminal`, `vt_factory`, and
     `vt_parser`.
   - `libvt.pytest_plugin` keeps module-level imports minimal and imports
     `Terminal` / `Parser` lazily inside fixture bodies so `pytest`
     collection pays near-zero overhead when `vt_*` fixtures are unused.
3. **Visual assertion helper (`libvt.testing.assert_screen`)**:
   - Renders expected vs actual screens inside Unicode box frames (`┌──┐` /
     `│..│` / `└──┘`) with `W` soft-wrap gutter markers and cursor / style
     diagnostics on mismatch, raising `ScreenAssertionError(AssertionError)`.

## Alternatives considered

- **Separate `pytest-libvt` PyPI package**: Splits documentation, versioning,
  and CI for a small pure-Python fixture module that already mirrors
  `libtmux.pytest_plugin` and `libvcs.pytest_plugin`.
- **Async methods directly on `Terminal`**: Mixes sans-I/O state mutation
  with event-loop scheduling and forces `import asyncio` into `terminal.py`.

## Consequences

### Positive

- `import libvt` stays fast and side-effect-free.
- `vt_*` fixtures auto-register when `libvt` is installed in a test
  environment without colliding with generic `terminal` or `parser`
  fixtures.
- Boxed `assert_screen` diffs show trailing spaces, soft-wrap boundaries,
  and cursor coordinates directly in pytest failure output.

### Tradeoffs

- Async callers import `AsyncTerminalStream` from `libvt.aio` rather than
  calling `await term.wait_for(...)` on `Terminal` directly.

### Risks

- Regressing import hygiene as new modules are added. Mitigation:
  `tests/test_libvt.py` asserts in a clean `sys.executable -c` subprocess
  that `asyncio` and `pytest` are absent from `sys.modules` after
  `import libvt`.
