(topics)=

# Topics

Guides covering `libvt`'s parser and screen design, synchronized redraws and
`asyncio` adapters, terminal protocols and input encoding, and migrating from
`pyte`.

::::{grid} 1 1 2 2
:gutter: 2 2 3 3

:::{grid-item-card} Architecture & Grid Storage
:link: architecture
:link-type: doc
How the parser, screen, terminal, and I/O adapters divide work, plus viewport, scrollback, and Unicode 17 storage.
:::

:::{grid-item-card} Synchronized Output & `asyncio`
:link: sync-and-asyncio
:link-type: doc
Holding completed frames during `?2026` redraws, {meth}`~libvt.terminal.Terminal.feed_until`,
{class}`~libvt.aio.AsyncTerminalStream`, and {func}`~libvt.aio.open_pty`.
:::

:::{grid-item-card} Protocols & Input Encoding
:link: protocols-and-input
:link-type: doc
Program status (`OSC 7501`), `OSC 133` prompt zones, terminal queries, keyboard/mouse modes, and paste encoding.
:::

:::{grid-item-card} Migrating from `pyte`
:link: migrating-from-pyte
:link-type: doc
Differences in terminal construction, coordinates, alternate screens, and query replies.
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
