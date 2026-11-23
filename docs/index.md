(index)=

# libvt

Pure-Python, zero-dependency, typed virtual terminal emulator and
streaming VT parser (`py3-none-any.whl` — no C shared library required).

::::{grid} 1 1 2 2
:gutter: 2 2 3 3

:::{grid-item-card} Quickstart
:link: quickstart
:link-type: doc
Render VT streams, inspect cells, and write boxed screen assertions.
:::

:::{grid-item-card} Topics
:link: topics/index
:link-type: doc
Sans-I/O layering, RLE scrollback, `?2026` stepping, `OSC 7501`, and input encoding.
:::

:::{grid-item-card} API Reference
:link: api/index
:link-type: doc
Public modules, dataclasses, protocols, and `vt_*` pytest fixtures.
:::

:::{grid-item-card} Project & ADRs
:link: project/index
:link-type: doc
Contributing, code style, releases, and Architecture Decision Records `0001`–`0010`.
:::

::::

## Install

```console
$ uv add libvt
```

```console
$ pip install libvt
```

`libvt` is pre-1.0; pin a minor range (`libvt>=0.1,<0.2`).

## At a glance

```python
>>> import libvt
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b[1;31mhello\x1b[0m \xe6\x97\xa5\xe6\x9c\xac")
>>> term.text
'hello 日本'
>>> term[0, 0].style.fg
<AnsiColor.RED: 1>
>>> term.cursor.row, term.cursor.col
(0, 10)
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
