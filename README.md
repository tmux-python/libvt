# libvt

`libvt` (`pip install libvt`, `import libvt`) is a pure-Python,
zero-dependency, typed virtual terminal emulator and streaming VT parser
(`py3-none-any.whl` — no C shared library required).

[![tests](https://github.com/tmux-python/libvt/actions/workflows/tests.yml/badge.svg)](https://github.com/tmux-python/libvt/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue)](https://github.com/tmux-python/libvt/blob/master/LICENSE)

## Requirements

- Python >= 3.12 (CPython and PyPy)
- Zero runtime dependencies

## Installation

Install with `uv`:

```console
$ uv add libvt
```

Install with `pip`:

```console
$ pip install libvt
```

`libvt` is pre-1.0; pin a minor range in downstream projects
(`libvt>=0.1,<0.2`).

## Quickstart

`libvt` performs no network or filesystem I/O and leaves `asyncio`
unloaded on `import libvt`:

```python
>>> import libvt
>>> libvt.__title__
'libvt'
```

## License

MIT License. See [LICENSE](LICENSE).
