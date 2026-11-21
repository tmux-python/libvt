(index)=

# libvt

Pure-Python, zero-dependency, typed virtual terminal emulator and
streaming VT parser (`py3-none-any.whl` — no C shared library required).

::::{grid} 1 1 3 3
:gutter: 2 2 3 3

:::{grid-item-card} Quickstart
:link: quickstart
:link-type: doc
Feed VT byte streams, slice 2D screen regions, and assert terminal state.
:::

:::{grid-item-card} Topics
:link: topics/index
:link-type: doc
Three-layer architecture, `?2026` sync frames, `asyncio`, and `pyte` migration.
:::

:::{grid-item-card} API Reference
:link: api/index
:link-type: doc
Every public class, dataclass, enum, and pytest fixture.
:::

:::{grid-item-card} Project
:link: project/index
:link-type: doc
Contributing, code style, benchmarks, releases, and ADRs.
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
>>> from libvt import AnsiColor, Terminal
>>> term = Terminal(rows=24, cols=80)
>>> term.feed(b"\x1b[1;32mOK\x1b[0m \xe6\x97\xa5\xe6\x9c\xac\r\n> ")
>>> term.text
'OK 日本\n> '
>>> (term[0, 0].text, term[0, 0].style.bold, term[0, 0].style.fg)
('O', True, <AnsiColor.GREEN: 2>)
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
