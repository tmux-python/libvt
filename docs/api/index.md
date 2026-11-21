(api)=

(reference)=

# API Reference

Public modules and types shipped by `libvt`.

## Core Modules

::::{grid} 1 1 2 2
:gutter: 2 2 3 3

:::{grid-item-card} `libvt`
:link: libvt
:link-type: doc
Top-level package exports and metadata.
:::

:::{grid-item-card} `libvt.terminal`
:link: terminal
:link-type: doc
`Terminal`, `Snapshot`, `Formatter`, `Effects`, `StopOn`, and `StepResult`.
:::

:::{grid-item-card} `libvt.screen`
:link: screen
:link-type: doc
`Screen`, `ScreenSet`, `Cell`, `RowView`, `RegionView`, and `Cursor`.
:::

:::{grid-item-card} `libvt.parser`
:link: parser
:link-type: doc
14-state `Parser`, `CsiParams`, `parse_sgr`, and typed `Action` events.
:::

:::{grid-item-card} `libvt.style`
:link: style
:link-type: doc
`Style`, `AnsiColor`, `IndexedColor`, `RgbColor`, `UnderlineStyle`, and `Hyperlink`.
:::

:::{grid-item-card} `libvt.modes`
:link: modes
:link-type: doc
`Mode`, `ModeState`, and `DecrpmValue`.
:::

::::

## Transports, Testing, and Exceptions

::::{grid} 1 2 2 2
:gutter: 2 2 3 3

:::{grid-item-card} `libvt.aio`
:link: aio
:link-type: doc
`AsyncTerminalStream` for `asyncio` readers and writers.
:::

:::{grid-item-card} `libvt.testing`
:link: testing
:link-type: doc
`assert_screen` and `format_screen_box` visual helpers.
:::

:::{grid-item-card} `libvt.pytest_plugin`
:link: pytest_plugin
:link-type: doc
`vt_terminal`, `vt_factory`, and `vt_parser` pytest fixtures.
:::

:::{grid-item-card} `libvt.exc`
:link: exc
:link-type: doc
`LibVtError`, `TerminalTimeoutError`, and `ScreenAssertionError`.
:::

::::

## Policy and Guarantees

::::{grid} 1 2 3 3
:gutter: 2

:::{grid-item-card} Public API
:link: ../project/public-api
:link-type: doc
What is and is not part of the public API.
:::

:::{grid-item-card} Compatibility
:link: ../project/compatibility
:link-type: doc
Supported Python versions and platforms.
:::

:::{grid-item-card} Deprecations
:link: ../project/deprecations
:link-type: doc
Active deprecations and migration guidance.
:::

::::

```{toctree}
:hidden:
:maxdepth: 1

libvt
terminal
screen
parser
style
modes
aio
testing
pytest_plugin
exc
```
