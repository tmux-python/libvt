"""Unicode cell width and grapheme-extension helpers."""

from __future__ import annotations

import enum
import unicodedata


class WidthRole(enum.IntEnum):
    """Column occupancy role of a terminal grid cell."""

    NARROW = 0
    WIDE = 1
    SPACER_TAIL = 2
    SPACER_HEAD = 3


def char_width(ch: str) -> int:
    """Return terminal column width (``0``, ``1``, or ``2``) for one codepoint."""
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E:
        return 1
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        return 0
    if unicodedata.combining(ch):
        return 0
    category = unicodedata.category(ch)
    if category in ("Mn", "Me", "Cf"):
        return 0
    # Regional indicators and skin-tone modifiers participate in clusters.
    if 0x1F3FB <= cp <= 0x1F3FF:
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    return 1


def is_grapheme_extender(
    ch: str,
    prev_cluster: str,
    *,
    mode_2027: bool,
) -> bool:
    """Return ``True`` when *ch* attaches to *prev_cluster* instead of advancing."""
    cp = ord(ch)
    if cp < 0x7F:
        return False
    if unicodedata.combining(ch):
        return True
    category = unicodedata.category(ch)
    if category in ("Mn", "Me"):
        return True
    # Variation selectors FE00..FE0F and E0100..E01EF.
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return True
    if not mode_2027:
        return False
    if cp == 0x200D:  # Zero-width joiner
        return True
    if 0x1F3FB <= cp <= 0x1F3FF:  # Emoji Fitzpatrick modifiers
        return True
    return bool(prev_cluster.endswith("\u200d"))


def cluster_prefers_wide(cluster: str, *, mode_2027: bool) -> bool:
    """Return ``True`` when an extended cluster widens from 1 to 2 columns."""
    if not mode_2027:
        return False
    return "\ufe0f" in cluster and "\ufe0e" not in cluster
