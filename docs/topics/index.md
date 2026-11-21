(topics)=

# Topics

Conceptual guides for `libvt`'s parser, screen representation, streaming
coordination, and migration path.

::::{grid} 1 1 3 3
:gutter: 2 2 3 3

:::{grid-item-card} Architecture
:link: architecture
:link-type: doc
Three-layer split (`Parser` -> `Effects` -> packed `Screen` / `Snapshot`).
:::

:::{grid-item-card} Sync Frames & Asyncio
:link: sync-and-asyncio
:link-type: doc
Mode `?2026` render holding, `feed_until`, `drain_replies`, and `libvt.aio`.
:::

:::{grid-item-card} Migrating from pyte
:link: migrating-from-pyte
:link-type: doc
Coordinate order, streaming ingestion, styles, and clean-room differences.
:::

::::

```{toctree}
:hidden:

architecture
sync-and-asyncio
migrating-from-pyte
```
