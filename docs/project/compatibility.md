# Compatibility

## Python

- **Minimum**: Python 3.12
- **Tested**: Python 3.12, 3.13, 3.14, 3.15
- **Maximum**: Python < 4.0
- **Runtimes**: CPython and PyPy

## Platforms

| Platform | Scope |
|----------|-------|
| Linux | Full suite including {func}`~libvt.aio.open_pty` |
| macOS | Full suite including {func}`~libvt.aio.open_pty` |
| WSL / WSL2 | Full suite including {func}`~libvt.aio.open_pty` |
| Windows | Terminal emulation, {class}`~libvt.aio.AsyncTerminalStream`, and `pytest` plugin ({func}`~libvt.aio.open_pty` requires POSIX) |
