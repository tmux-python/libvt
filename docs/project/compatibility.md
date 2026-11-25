# Compatibility

## Python

- **Minimum**: Python 3.12
- **Tested**: Python 3.12, 3.13, 3.14, 3.15
- **Maximum**: Python < 4.0
- **Runtimes**: CPython and PyPy (`py3-none-any.whl`, zero C extensions)

## Platforms

| Platform | Scope |
|----------|-------|
| Linux | Full suite including {func}`~libvt.aio.open_pty` |
| macOS | Full suite including {func}`~libvt.aio.open_pty` |
| WSL / WSL2 | Full suite including {func}`~libvt.aio.open_pty` |
| Windows | Sans-I/O core, {class}`~libvt.aio.AsyncTerminalStream`, and `pytest` plugin |
