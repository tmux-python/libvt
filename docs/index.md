(index)=

# libvt

Headless terminal emulator and VT parser for Python. Feed it terminal output to
inspect screen contents, cursor positions, and cell styles, or use it to test
terminal programs.

Requires Python 3.12 or later (CPython and PyPy). Written in Python, with type
annotations and no runtime dependencies.

::::{grid} 1 1 2 2
:gutter: 2 2 3 3

:::{grid-item-card} Quickstart
:link: quickstart
:link-type: doc
Feed terminal output, inspect cells and styles, and assert screen state.
:::

:::{grid-item-card} Topics
:link: topics/index
:link-type: doc
Architecture and scrollback, synchronized redraws (`?2026`), protocols and input, and migrating from `pyte`.
:::

:::{grid-item-card} API Reference
:link: api/index
:link-type: doc
Public modules, classes, functions, and `vt_*` pytest fixtures.
:::

:::{grid-item-card} Project & ADRs
:link: project/index
:link-type: doc
Contributing, code style, releases, and Architecture Decision Records `0001`–`0010`.
:::

::::

## Install

```console
$ uv add "libvt>=0.0.1a1,<0.1"
```

```console
$ pip install "libvt>=0.0.1a1,<0.1"
```

`libvt` is in pre-alpha (`0.0.1a1`). Minor and `0.0.x` releases may include
breaking API changes; pin `libvt>=0.0.1a1,<0.1` while the package is pre-1.0.

## At a glance

```python
>>> import libvt
>>> term = libvt.Terminal(rows=2, cols=20)
>>> term.feed(b"Loading 10%")
>>> term.feed(b"\r\x1b[2KLoading 20%")
>>> term.text
'Loading 20%'
>>> term.cursor.row, term.cursor.col
(0, 11)
```

```{toctree}
:hidden:

quickstart
topics/index
api/index
project/index
history
migration
GitHub <https://github.com/tmux-python/libvt>
```
