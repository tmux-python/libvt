(topics)=

# Topics

Conceptual guides covering `libvt`'s sans-I/O architecture, synchronize
stepping, terminal protocols, and migration patterns.

::::{grid} 1 1 2 2
:gutter: 2 2 3 3

:::{grid-item-card} Architecture & Grid Storage
:link: architecture
:link-type: doc
Sans-I/O layering, lazy 64-bit viewport rows, RLE scrollback, and Unicode 17 clustering.
:::

:::{grid-item-card} Synchronized Output & `asyncio`
:link: sync-and-asyncio
:link-type: doc
DEC `?2026` copy-on-write holds, {meth}`~libvt.terminal.Terminal.feed_until`,
{class}`~libvt.aio.AsyncTerminalStream`, and {func}`~libvt.aio.open_pty`.
:::

:::{grid-item-card} Protocols & Input Encoding
:link: protocols-and-input
:link-type: doc
Rex `OSC 7501` Program Status, `OSC 133`, host queries, Kitty `CSI u`, and paste safety.
:::

:::{grid-item-card} Migrating from `pyte`
:link: migrating-from-pyte
:link-type: doc
Translating screen setup, coordinates, alternate screens, and replies to `libvt`.
:::

::::

```{toctree}
:hidden:
:maxdepth: 2

architecture
sync-and-asyncio
protocols-and-input
migrating-from-pyte
```
