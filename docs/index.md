(index)=

# libvt

Pure-Python, zero-dependency, typed virtual terminal emulator and
streaming VT parser (`py3-none-any.whl` — no C shared library required).

::::{grid} 1 1 3 3
:gutter: 2 2 3 3

:::{grid-item-card} Quickstart
:link: quickstart
:link-type: doc
Install `libvt` and inspect package metadata.
:::

:::{grid-item-card} Topics
:link: topics/index
:link-type: doc
Parser state machine, screen storage, and sans-I/O design.
:::

:::{grid-item-card} API Reference
:link: api/index
:link-type: doc
Public modules, types, and pytest fixtures.
:::

:::{grid-item-card} Project
:link: project/index
:link-type: doc
Contributing, code style, releases, and architecture decisions.
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
>>> libvt.__title__
'libvt'
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
