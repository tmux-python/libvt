(api)=

(reference)=

# API Reference

Public modules and types shipped by `libvt`.

::::{grid} 1 1 2 2
:gutter: 2 2 3 3

:::{grid-item-card} `libvt`
:link: libvt
:link-type: doc
Top-level re-exports, progressive-disclosure helpers, and package metadata.
:::

:::{grid-item-card} `libvt.terminal`
:link: terminal
:link-type: doc
`Terminal`, `Snapshot`, `Formatter`, `FlightRecorder`, `render`, and `strip`.
:::

:::{grid-item-card} `libvt.screen`
:link: screen
:link-type: doc
`Screen`, `ScreenSet`, `Cell`, `RowView`, `RowRun`, `RegionView`, and `Cursor`.
:::

:::{grid-item-card} `libvt.parser`
:link: parser
:link-type: doc
Streaming VT500 `Parser`, `CsiParams`, `Action` records, and `parse_sgr`.
:::

:::{grid-item-card} `libvt.style` & `libvt.modes`
:link: style
:link-type: doc
`Style`, `AnsiColor`, `IndexedColor`, `RgbColor`, `Hyperlink`, and DEC/ANSI `Mode`s.
:::

:::{grid-item-card} `libvt.config`, `events`, & `status`
:link: config
:link-type: doc
`Profile`, `Policy`, `Limits`, closed `Event` union, and `OSC 7501` `ProgramStatus`.
:::

:::{grid-item-card} `libvt.input` & `libvt.codec`
:link: input
:link-type: doc
Mode-aware `KeyEvent`, `MouseEvent`, `PasteEvent`, and VT sequence `explain`.
:::

:::{grid-item-card} `libvt.aio`, `testing`, & `pytest_plugin`
:link: aio
:link-type: doc
`AsyncTerminalStream`, POSIX `open_pty`, `assert_screen`, and `vt_*` fixtures.
:::

:::{grid-item-card} `libvt.graphics`
:link: graphics
:link-type: doc
Zero-dependency `SIXEL` and Kitty `APC G` `ImageMeta` and `ImagePlacement`.
:::

:::{grid-item-card} `libvt.tmux`
:link: tmux
:link-type: doc
`decode_tmux_capture`, `TmuxPaneMetadata`, and `-CC` `%output` unescaping.
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
config
events
status
input
codec
graphics
tmux
aio
testing
pytest_plugin
exc
```
