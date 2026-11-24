"""Zero-dependency sans-I/O ``SIXEL`` and Kitty ``APC G`` placement tracker."""

from __future__ import annotations

import base64
import binascii
import collections
import collections.abc
import enum
import struct
import typing as t
import zlib
from dataclasses import dataclass

from libvt.parser import _parse_ascii_uint

if t.TYPE_CHECKING:
    from libvt.config import SixelErasePolicy

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_Z_BELOW_BG_THRESHOLD = -(1 << 30)
_MAX_UPLOAD_CHUNKS = 4_096
_MAX_UPLOAD_BYTES = 16_777_216
_MAX_ZLIB_INFLATE_BYTES = 16_777_216
_ZLIB_STEP_BYTES = 65_536
_MAX_MASK_CELLS = 16_384
_MAX_PARENT_DEPTH = 8


class GraphicsProtocol(enum.StrEnum):
    """Terminal graphics protocol family."""

    SIXEL = "sixel"
    KITTY = "kitty"


class ZLayer(enum.StrEnum):
    """Kitty graphics z-ordering plane relative to cell background and text."""

    BELOW_BG = "below_bg"
    BELOW_TEXT = "below_text"
    ABOVE_TEXT = "above_text"


@dataclass(frozen=True, slots=True)
class ImageMeta:
    """Stored sans-I/O image metadata without buffered pixel bitmaps.

    Attributes
    ----------
    protocol : GraphicsProtocol
        Originating graphics protocol (``SIXEL`` or ``KITTY``).
    image_id : int
        Protocol image identifier (auto-assigned positive integer for ``SIXEL``
        or unnumbered Kitty uploads).
    image_number : int
        Optional Kitty ``I=`` non-unique image number (``0`` when unset).
    width_px : int
        Decoded or declared image width in pixels.
    height_px : int
        Decoded or declared image height in pixels.
    format_code : int
        Kitty ``f=`` pixel format (``8``, ``16``, ``24``, ``32``, or ``100``
        PNG), or ``0`` for ``SIXEL``.
    compressed : bool
        ``True`` when transmitted with Kitty ``o=z`` zlib compression.
    byte_len : int
        Total encoded payload byte count observed across upload chunks.
    """

    protocol: GraphicsProtocol
    image_id: int
    image_number: int = 0
    width_px: int = 0
    height_px: int = 0
    format_code: int = 32
    compressed: bool = False
    byte_len: int = 0


@dataclass(frozen=True, slots=True)
class ImagePlacement:
    """Immutable snapshot of a ``SIXEL`` or Kitty image placement on a screen.

    Attributes
    ----------
    protocol : GraphicsProtocol
        Originating graphics protocol.
    image_id : int
        Referenced :attr:`ImageMeta.image_id`.
    placement_id : int
        Kitty ``p=`` placement identifier (``0`` when default/unnumbered).
    abs_row : int
        Monotonic absolute grid row anchor.
    col : int
        Zero-based starting column.
    rows : int
        Height in terminal cells.
    cols : int
        Width in terminal cells.
    z_index : int
        Signed 32-bit z-index (``0`` default).
    virtual : bool
        ``True`` for Kitty ``U=1`` Unicode-placeholder virtual placements.
    parent_image_id : int
        Parent image ID for Kitty ``P=`` relative placements (``0`` when none).
    parent_placement_id : int
        Parent placement ID for Kitty ``Q=`` relative placements.
    source_x : int
        Cropped source rectangle X origin in pixels.
    source_y : int
        Cropped source rectangle Y origin in pixels.
    source_width : int
        Cropped source rectangle width in pixels.
    source_height : int
        Cropped source rectangle height in pixels.
    cell_offset_x : int
        Sub-cell pixel X offset within the anchor cell.
    cell_offset_y : int
        Sub-cell pixel Y offset within the anchor cell.
    visible_mask : int | None
        Bitmask of ``rows * cols`` visible cells for ``SIXEL`` under
        ``"cell_mask"`` erase policy (bit ``r * cols + c`` is ``1`` when
        intact); ``None`` when all cells in ``(rows, cols)`` are intact.
    """

    protocol: GraphicsProtocol
    image_id: int
    placement_id: int
    abs_row: int
    col: int
    rows: int
    cols: int
    z_index: int = 0
    virtual: bool = False
    parent_image_id: int = 0
    parent_placement_id: int = 0
    source_x: int = 0
    source_y: int = 0
    source_width: int = 0
    source_height: int = 0
    cell_offset_x: int = 0
    cell_offset_y: int = 0
    visible_mask: int | None = None

    @property
    def layer(self) -> ZLayer:
        """Return rendering plane derived from :attr:`z_index`."""
        if self.virtual:
            return ZLayer.BELOW_TEXT
        if self.z_index < _Z_BELOW_BG_THRESHOLD:
            return ZLayer.BELOW_BG
        if self.z_index < 0:
            return ZLayer.BELOW_TEXT
        return ZLayer.ABOVE_TEXT

    def occupies_abs(self, abs_row: int, col: int) -> bool:
        """Return ``True`` when absolute grid cell ``(abs_row, col)`` is covered.

        Parameters
        ----------
        abs_row : int
            Monotonic absolute row coordinate (for a viewport row ``0 <= r < rows``,
            pass ``snap.scrolled_total + r``).
        col : int
            Zero-based column coordinate.

        Returns
        -------
        bool
            ``True`` when this non-virtual placement covers ``(abs_row, col)``
            and its cell mask bit (if any) remains set; ``False`` for virtual
            placements or uncovered cells.

        Raises
        ------
        TypeError
            When *abs_row* or *col* is not an :class:`int` (or is a :class:`bool`).
        """
        if isinstance(abs_row, bool) or not isinstance(abs_row, int):
            msg = f"abs_row must be int, got {type(abs_row).__name__}"
            raise TypeError(msg)
        if isinstance(col, bool) or not isinstance(col, int):
            msg = f"col must be int, got {type(col).__name__}"
            raise TypeError(msg)
        if self.virtual:
            return False
        if not (
            self.abs_row <= abs_row < self.abs_row + self.rows
            and self.col <= col < self.col + self.cols
        ):
            return False
        if self.visible_mask is None:
            return True
        bit = (abs_row - self.abs_row) * self.cols + (col - self.col)
        return bool((self.visible_mask >> bit) & 1)


def extract_png_dimensions(header_bytes: bytes) -> tuple[int, int] | None:
    """Extract ``(width_px, height_px)`` from the first 24 bytes of a PNG stream.

    Parameters
    ----------
    header_bytes : bytes
        Leading uncompressed bytes of a PNG payload (at least 24 bytes).

    Returns
    -------
    tuple[int, int] | None
        ``(width_px, height_px)`` when the PNG signature and ``IHDR`` chunk
        header are valid and positive, else ``None``.
    """
    if len(header_bytes) < 24 or header_bytes[:8] != _PNG_SIGNATURE:
        return None
    ihdr_len, chunk_type = struct.unpack(">I4s", header_bytes[8:16])
    if chunk_type != b"IHDR" or ihdr_len < 13:
        return None
    width_px, height_px = struct.unpack(">II", header_bytes[16:24])
    if width_px <= 0 or height_px <= 0:
        return None
    return width_px, height_px


def scan_sixel_geometry(
    payload: bytes,
    *,
    max_dimension: int = 10_000,
) -> tuple[int, int]:
    """Compute ``(width_px, height_px)`` of a ``SIXEL`` payload in ``O(n)`` time.

    Parameters
    ----------
    payload : bytes
        Raw ``SIXEL`` data stream following ``DCS Ps... q``.
    max_dimension : int
        Upper bound applied to raster attributes and ``!Pn`` repeat counts.

    Returns
    -------
    tuple[int, int]
        Clamped ``(width_px, height_px)`` in pixels.
    """
    n = len(payload)
    idx = 0
    raster_w = 0
    raster_h = 0
    cur_x = 0
    cur_y = 0
    max_x = 0
    max_y = 0

    while idx < n:
        b = payload[idx]
        if 0x3F <= b <= 0x7E:
            cur_x = min(max_dimension, cur_x + 1)
            max_x = max(max_x, cur_x)
            max_y = max(max_y, min(max_dimension, cur_y + 6))
            idx += 1
        elif b == 0x21:  # '!' repeat introducer: !Pn<sixel>
            idx += 1
            rep = 0
            has_digits = False
            while idx < n and 0x30 <= payload[idx] <= 0x39:
                has_digits = True
                rep = min(max_dimension, rep * 10 + (payload[idx] - 0x30))
                idx += 1
            if idx < n and 0x3F <= payload[idx] <= 0x7E:
                count = max(1, rep) if has_digits else 1
                cur_x = min(max_dimension, cur_x + count)
                max_x = max(max_x, cur_x)
                max_y = max(max_y, min(max_dimension, cur_y + 6))
                idx += 1
        elif b == 0x22:  # '"' raster attributes: "Pan;Pad;Ph;Pv
            idx += 1
            nums: list[int] = []
            cur_num = 0
            while idx < n and (0x30 <= payload[idx] <= 0x39 or payload[idx] == 0x3B):
                if payload[idx] == 0x3B:
                    nums.append(cur_num)
                    cur_num = 0
                else:
                    cur_num = min(max_dimension, cur_num * 10 + (payload[idx] - 0x30))
                idx += 1
            nums.append(cur_num)
            if len(nums) >= 4:
                raster_w = min(max_dimension, nums[2])
                raster_h = min(max_dimension, nums[3])
        elif b == 0x23:  # '#' color introducer: #Pc or #Pc;Pu;Px;Py;Pz
            idx += 1
            while idx < n and (0x30 <= payload[idx] <= 0x39 or payload[idx] == 0x3B):
                idx += 1
        elif b == 0x24:  # '$' graphics carriage return
            cur_x = 0
            idx += 1
        elif b == 0x2D:  # '-' graphics new line
            cur_x = 0
            cur_y = min(max_dimension, cur_y + 6)
            idx += 1
        else:
            idx += 1

    return min(max_dimension, max(raster_w, max_x)), min(
        max_dimension, max(raster_h, max_y)
    )


@dataclass(slots=True)
class _MutablePlacement:
    protocol: GraphicsProtocol
    image_id: int
    placement_id: int
    abs_row: int
    col: int
    rows: int
    cols: int
    z_index: int = 0
    virtual: bool = False
    parent_image_id: int = 0
    parent_placement_id: int = 0
    source_x: int = 0
    source_y: int = 0
    source_width: int = 0
    source_height: int = 0
    cell_offset_x: int = 0
    cell_offset_y: int = 0
    visible_mask: int | None = None

    def freeze(self) -> ImagePlacement:
        return ImagePlacement(
            protocol=self.protocol,
            image_id=self.image_id,
            placement_id=self.placement_id,
            abs_row=self.abs_row,
            col=self.col,
            rows=self.rows,
            cols=self.cols,
            z_index=self.z_index,
            virtual=self.virtual,
            parent_image_id=self.parent_image_id,
            parent_placement_id=self.parent_placement_id,
            source_x=self.source_x,
            source_y=self.source_y,
            source_width=self.source_width,
            source_height=self.source_height,
            cell_offset_x=self.cell_offset_x,
            cell_offset_y=self.cell_offset_y,
            visible_mask=self.visible_mask,
        )


def _trim_masked_sixel_rows(p: _MutablePlacement) -> bool:
    """Trim completely zeroed leading or trailing ``SIXEL`` mask rows in place."""
    if p.protocol != GraphicsProtocol.SIXEL or p.visible_mask is None:
        return False
    if p.rows <= 0 or p.cols <= 0 or p.visible_mask <= 0:
        p.rows = 0
        p.visible_mask = 0
        return True
    cols = p.cols
    mask = p.visible_mask
    row_full = (1 << cols) - 1
    top_trim = 0
    while top_trim < p.rows and ((mask >> (top_trim * cols)) & row_full) == 0:
        top_trim += 1
    bot_trim = 0
    while (
        top_trim + bot_trim < p.rows
        and ((mask >> ((p.rows - 1 - bot_trim) * cols)) & row_full) == 0
    ):
        bot_trim += 1
    if top_trim == 0 and bot_trim == 0:
        if mask == (1 << (p.rows * cols)) - 1:
            p.visible_mask = None
            return True
        return False
    if top_trim + bot_trim >= p.rows:
        p.rows = 0
        p.visible_mask = 0
        return True
    if top_trim > 0:
        if p.source_height > 0:
            crop_top = (p.source_height * top_trim) // p.rows
            p.source_y += crop_top
            p.source_height = max(1, p.source_height - crop_top)
        p.abs_row += top_trim
        p.rows -= top_trim
        mask >>= top_trim * cols
    if bot_trim > 0:
        if p.source_height > 0 and p.rows > 0:
            crop_bot = (p.source_height * bot_trim) // p.rows
            p.source_height = max(1, p.source_height - crop_bot)
        p.rows -= bot_trim
        mask &= (1 << (p.rows * cols)) - 1
    p.visible_mask = None if mask == (1 << (p.rows * cols)) - 1 else mask
    return True


def _clip_placement_bounds(
    p: _MutablePlacement,
    *,
    min_abs: int,
    max_abs: int | None,
    new_cols: int | None,
) -> tuple[bool, bool]:
    """Clip *p* to ``[min_abs, max_abs)`` and *new_cols*; return ``(mutated, keep)``."""
    mutated = False
    if p.rows <= 0 or p.cols <= 0:
        return True, False
    if new_cols is not None:
        if p.col >= new_cols:
            return True, False
        if p.protocol == GraphicsProtocol.SIXEL and p.col + p.cols > new_cols:
            keep_cols = new_cols - p.col
            if keep_cols <= 0:
                return True, False
            old_cols = p.cols
            if p.visible_mask is not None and p.rows * old_cols <= _MAX_MASK_CELLS:
                row_keep = (1 << keep_cols) - 1
                new_mask = 0
                for rr in range(p.rows):
                    row_bits = (p.visible_mask >> (rr * old_cols)) & row_keep
                    new_mask |= row_bits << (rr * keep_cols)
                if new_mask == 0:
                    return True, False
                full_new = (1 << (p.rows * keep_cols)) - 1
                p.visible_mask = None if new_mask == full_new else new_mask
            if p.source_width > 0:
                p.source_width = max(1, (p.source_width * keep_cols) // old_cols)
            p.cols = keep_cols
            mutated = True

    if p.abs_row < min_abs:
        clip_top = min_abs - p.abs_row
        if clip_top >= p.rows:
            return True, False
        if p.source_height > 0:
            crop_px = (p.source_height * clip_top) // p.rows
            p.source_y += crop_px
            p.source_height = max(1, p.source_height - crop_px)
        keep_rows = p.rows - clip_top
        if p.visible_mask is not None and p.rows * p.cols <= _MAX_MASK_CELLS:
            shifted = p.visible_mask >> (clip_top * p.cols)
            full_m = (1 << (keep_rows * p.cols)) - 1
            shifted &= full_m
            if shifted == 0:
                return True, False
            p.visible_mask = None if shifted == full_m else shifted
        p.rows = keep_rows
        p.abs_row = min_abs
        mutated = True

    if max_abs is not None and p.abs_row + p.rows > max_abs:
        if p.abs_row >= max_abs:
            return True, False
        keep_rows = max_abs - p.abs_row
        clip_bot = p.rows - keep_rows
        if p.source_height > 0:
            crop_px = (p.source_height * clip_bot) // p.rows
            p.source_height = max(1, p.source_height - crop_px)
        if p.visible_mask is not None and keep_rows * p.cols <= _MAX_MASK_CELLS:
            full_m = (1 << (keep_rows * p.cols)) - 1
            masked = p.visible_mask & full_m
            if masked == 0:
                return True, False
            p.visible_mask = None if masked == full_m else masked
        p.rows = keep_rows
        mutated = True

    if _trim_masked_sixel_rows(p):
        mutated = True
        if p.rows <= 0:
            return True, False
    return mutated, True


class _Decompressor(t.Protocol):
    @property
    def eof(self) -> bool: ...

    @property
    def unconsumed_tail(self) -> bytes: ...

    def decompress(self, data: bytes, /, max_length: int = 0) -> bytes: ...

    def flush(self, length: int = ..., /) -> bytes: ...


@dataclass(slots=True)
class _PendingKittyUpload:
    control: dict[str, str]
    b64_rem: bytes
    header_buf: bytearray
    decompressor: _Decompressor | None
    inflate_cap: int
    encoded_bytes: int
    decoded_bytes: int
    decompressed_bytes: int
    chunks: int
    failed_error: str | None = None


_KITTY_UINT32_MAX = 0xFFFF_FFFF
_KITTY_INT32_MIN = -2_147_483_648
_KITTY_INT32_MAX = 2_147_483_647


def _parse_uint32(control: dict[str, str], key: str, default: int = 0) -> int:
    raw = control.get(key)
    if raw is None or not raw:
        return default
    val = _parse_ascii_uint(raw, max_val=_KITTY_UINT32_MAX)
    return default if val is None else val


def _parse_int(control: dict[str, str], key: str, default: int = 0) -> int:
    raw = control.get(key)
    if raw is None or not raw:
        return default
    neg = False
    start = 0
    first = raw[0]
    if first == "-":
        neg = True
        start = 1
    elif first == "+":
        start = 1
    if start >= len(raw):
        return default
    limit = -_KITTY_INT32_MIN if neg else _KITTY_UINT32_MAX
    acc = 0
    for idx in range(start, len(raw)):
        code = ord(raw[idx])
        if not (0x30 <= code <= 0x39):
            return default
        if acc < limit:
            acc = min(limit, acc * 10 + (code - 0x30))
    return -acc if neg else acc


def _valid_quiet(control: dict[str, str]) -> int:
    raw = control.get("q")
    if raw == "2":
        return 2
    if raw == "1":
        return 1
    return 0


def _kitty_reply(
    control: dict[str, str],
    status: str,
    *,
    is_error: bool,
    reply_image_id: int | None = None,
) -> bytes | None:
    quiet = _valid_quiet(control)
    if quiet == 2 or (quiet == 1 and not is_error):
        return None
    client_id = _parse_uint32(control, "i", 0)
    client_num = _parse_uint32(control, "I", 0)
    if client_id <= 0 and client_num <= 0:
        return None
    img_id = reply_image_id if reply_image_id is not None else client_id
    pid = _parse_uint32(control, "p", 0)
    tokens: list[str] = []
    if img_id > 0:
        tokens.append(f"i={img_id}")
    if client_num > 0:
        tokens.append(f"I={client_num}")
    if pid > 0:
        tokens.append(f"p={pid}")
    head = ",".join(tokens)
    return f"\x1b_G{head};{status}\x1b\\".encode("ascii")


class GraphicsTracker:
    """Per-screen sans-I/O ``SIXEL`` and Kitty ``APC G`` state machine.

    Attributes
    ----------
    has_sixels : bool
        ``True`` when at least one active or scrollback ``SIXEL`` placement is
        present, gating cell-overwrite hooks in ``O(1)``.
    revision : int
        Monotonic counter incremented whenever stored placements change.
    """

    __slots__ = (
        "_images",
        "_max_dim",
        "_max_images",
        "_max_placements",
        "_next_auto_id",
        "_pending",
        "_placements",
        "has_sixels",
        "revision",
    )

    def __init__(
        self,
        *,
        max_images: int = 256,
        max_placements: int = 1_024,
        max_image_dimension: int = 10_000,
    ) -> None:
        self._max_images = max(1, max_images)
        self._max_placements = max(1, max_placements)
        self._max_dim = max(1, max_image_dimension)
        self._images: collections.OrderedDict[int, ImageMeta] = (
            collections.OrderedDict()
        )
        self._placements: list[_MutablePlacement] = []
        self._pending: _PendingKittyUpload | None = None
        self._next_auto_id: int = 0x8000_0001
        self.has_sixels: bool = False
        self.revision: int = 0

    def _bump(self) -> None:
        self.revision += 1
        self.has_sixels = any(
            p.protocol == GraphicsProtocol.SIXEL for p in self._placements
        )

    def clone(self) -> GraphicsTracker:
        """Return an independent copy of this tracker."""
        dup = GraphicsTracker(
            max_images=self._max_images,
            max_placements=self._max_placements,
            max_image_dimension=self._max_dim,
        )
        dup._images = self._images.copy()
        dup._placements = [
            _MutablePlacement(
                protocol=p.protocol,
                image_id=p.image_id,
                placement_id=p.placement_id,
                abs_row=p.abs_row,
                col=p.col,
                rows=p.rows,
                cols=p.cols,
                z_index=p.z_index,
                virtual=p.virtual,
                parent_image_id=p.parent_image_id,
                parent_placement_id=p.parent_placement_id,
                source_x=p.source_x,
                source_y=p.source_y,
                source_width=p.source_width,
                source_height=p.source_height,
                cell_offset_x=p.cell_offset_x,
                cell_offset_y=p.cell_offset_y,
                visible_mask=p.visible_mask,
            )
            for p in self._placements
        ]
        dup._next_auto_id = self._next_auto_id
        dup.has_sixels = self.has_sixels
        dup.revision = self.revision
        return dup

    @property
    def images(self) -> dict[int, ImageMeta]:
        """Return shallow copy of stored :class:`ImageMeta` entries."""
        return dict(self._images)

    def snapshot_placements(self) -> tuple[ImagePlacement, ...]:
        """Freeze stored placements into immutable :class:`ImagePlacement` records."""
        if not self._placements:
            return ()
        return tuple(p.freeze() for p in self._placements)

    def has_placement_on_row(self, row: int, scrolled_total: int) -> bool:
        """Return ``True`` when any non-virtual placement covers viewport *row*."""
        abs_y = scrolled_total + row
        return any(
            not p.virtual and p.abs_row <= abs_y < p.abs_row + p.rows
            for p in self._placements
        )

    def _store_image(self, meta: ImageMeta) -> None:
        had_placements = False
        if meta.image_id in self._images:
            self._images.move_to_end(meta.image_id)
            before = len(self._placements)
            self._placements = [
                p for p in self._placements if p.image_id != meta.image_id
            ]
            had_placements = len(self._placements) != before
        self._images[meta.image_id] = meta
        evicted_any = False
        while len(self._images) > self._max_images:
            evicted_id, _ = self._images.popitem(last=False)
            before = len(self._placements)
            self._placements = [p for p in self._placements if p.image_id != evicted_id]
            if len(self._placements) != before:
                evicted_any = True
        if had_placements or evicted_any:
            self._prune_orphans()
            self._bump()

    def _append_placement(self, pl: _MutablePlacement) -> None:
        replaced = False
        if pl.protocol == GraphicsProtocol.KITTY and pl.placement_id > 0:
            before = len(self._placements)
            self._placements = [
                p
                for p in self._placements
                if not (
                    p.protocol == GraphicsProtocol.KITTY
                    and p.image_id == pl.image_id
                    and p.placement_id == pl.placement_id
                )
            ]
            replaced = len(self._placements) != before
        self._placements.append(pl)
        need_prune = replaced
        if len(self._placements) > self._max_placements:
            del self._placements[: len(self._placements) - self._max_placements]
            need_prune = True
        if need_prune:
            self._prune_orphans()
        self._bump()

    def reset(self) -> None:
        """Clear all stored images, placements, and in-progress uploads."""
        if self._images or self._placements or self._pending is not None:
            self._images.clear()
            self._placements.clear()
            self._pending = None
            self.has_sixels = False
            self.revision += 1

    def clear_active_screen(self, scrolled_total: int, screen_rows: int) -> None:
        """Delete non-virtual placements intersecting the active viewport."""
        if not self._placements:
            return
        top_abs = scrolled_total
        bot_abs = scrolled_total + screen_rows
        kept = [
            p
            for p in self._placements
            if p.virtual or p.abs_row + p.rows <= top_abs or p.abs_row >= bot_abs
        ]
        if len(kept) != len(self._placements):
            self._placements = kept
            self._prune_orphans()
            self._bump()

    def clear_history(self, scrolled_total: int) -> None:
        """Clip or delete placements intersecting cleared scrollback (``ED 3``)."""
        self.reap_evicted_history(scrolled_total)

    def reap_evicted_history(self, min_retained_abs_row: int) -> None:
        """Clip or reap non-virtual placements scrolled past *min_retained_abs_row*."""
        if not self._placements:
            return
        changed = False
        kept: list[_MutablePlacement] = []
        for p in self._placements:
            if p.virtual:
                kept.append(p)
                continue
            mutated, keep = _clip_placement_bounds(
                p,
                min_abs=min_retained_abs_row,
                max_abs=None,
                new_cols=None,
            )
            if mutated:
                changed = True
            if keep:
                kept.append(p)
            else:
                changed = True
        if changed:
            self._placements = kept
            self._prune_orphans()
            self._bump()

    def root_placement_pins(self) -> list[tuple[int, int, int]]:
        """Return ``(index, abs_row, col)`` for non-virtual root placements."""
        return [
            (idx, p.abs_row, p.col)
            for idx, p in enumerate(self._placements)
            if not p.virtual and p.parent_image_id == 0
        ]

    def apply_reflow_pins(
        self,
        remapped_roots: collections.abc.Sequence[tuple[int, int, int]],
        *,
        new_cols: int,
        min_retained_abs_row: int,
        max_abs: int,
    ) -> None:
        """Apply reflowed ``(index, abs_row, col)`` pins and clip screen bounds."""
        if not self._placements:
            return
        n_pl = len(self._placements)
        old_coords: list[tuple[int, int]] = [
            (p.abs_row, p.col) for p in self._placements
        ]
        last_by_key: dict[tuple[int, int], int] = {}
        last_by_img: dict[int, int] = {}
        for idx, p in enumerate(self._placements):
            if not p.virtual:
                last_by_img[p.image_id] = idx
                if p.placement_id > 0:
                    last_by_key[(p.image_id, p.placement_id)] = idx

        resolved: list[bool] = [
            p.virtual or p.parent_image_id <= 0 for p in self._placements
        ]
        changed = False
        for idx, new_abs, new_col in remapped_roots:
            p = self._placements[idx]
            clamped_abs = max(0, new_abs)
            clamped_col = max(0, min(new_cols - 1, new_col))
            if p.abs_row != clamped_abs or p.col != clamped_col:
                p.abs_row = clamped_abs
                p.col = clamped_col
                changed = True

        def _resolve_child(idx: int, depth: int) -> None:
            nonlocal changed
            if resolved[idx] or depth > _MAX_PARENT_DEPTH:
                resolved[idx] = True
                return
            p = self._placements[idx]
            p_idx = (
                last_by_key.get((p.parent_image_id, p.parent_placement_id))
                if p.parent_placement_id > 0
                else last_by_img.get(p.parent_image_id)
            )
            if p_idx is None or p_idx == idx:
                resolved[idx] = True
                return
            if not resolved[p_idx]:
                _resolve_child(p_idx, depth + 1)
            parent_pl = self._placements[p_idx]
            rel_v = old_coords[idx][0] - old_coords[p_idx][0]
            rel_h = old_coords[idx][1] - old_coords[p_idx][1]
            new_abs = max(0, parent_pl.abs_row + rel_v)
            new_col = max(0, min(new_cols - 1, parent_pl.col + rel_h))
            if p.abs_row != new_abs or p.col != new_col:
                p.abs_row = new_abs
                p.col = new_col
                changed = True
            resolved[idx] = True

        for idx in range(n_pl):
            if not resolved[idx]:
                _resolve_child(idx, 0)

        kept: list[_MutablePlacement] = []
        dropped_any = False
        for p in self._placements:
            if p.virtual:
                kept.append(p)
                continue
            mutated, keep = _clip_placement_bounds(
                p,
                min_abs=min_retained_abs_row,
                max_abs=max_abs,
                new_cols=new_cols,
            )
            if mutated:
                changed = True
            if keep:
                kept.append(p)
            else:
                changed = True
                dropped_any = True
        if dropped_any:
            self._placements = kept
            self._prune_orphans()
        if changed:
            self._bump()

    def on_non_reflow_resize(
        self,
        *,
        top_drop_without_history: int,
        new_rows: int,
        new_cols: int,
        scrolled_total: int,
        min_retained_abs_row: int = 0,
    ) -> None:
        """Adjust placements when a non-reflow ``resize()`` drops or clips rows."""
        if not self._placements:
            return
        changed = False
        kept: list[_MutablePlacement] = []
        dropped_any = False
        max_abs = scrolled_total + new_rows
        for p in self._placements:
            if p.virtual:
                kept.append(p)
                continue
            if top_drop_without_history > 0:
                p.abs_row -= top_drop_without_history
                changed = True
            eff_min = max(
                min_retained_abs_row,
                scrolled_total if top_drop_without_history > 0 else 0,
            )
            mutated, keep = _clip_placement_bounds(
                p,
                min_abs=eff_min,
                max_abs=max_abs,
                new_cols=new_cols,
            )
            if mutated:
                changed = True
            if keep:
                kept.append(p)
            else:
                changed = True
                dropped_any = True
        if dropped_any:
            self._placements = kept
            self._prune_orphans()
        if changed:
            self._bump()

    def erase_sixel_rect(
        self,
        *,
        row: int,
        col: int,
        rows: int,
        cols: int,
        scrolled_total: int,
        policy: SixelErasePolicy,
    ) -> None:
        """Clip or remove ``SIXEL`` placements intersected by a cell edit/erase."""
        if not self.has_sixels or rows <= 0 or cols <= 0:
            return
        r0 = scrolled_total + row
        r1 = r0 + rows
        c0 = col
        c1 = col + cols
        changed = False
        kept: list[_MutablePlacement] = []
        for p in self._placements:
            if p.protocol != GraphicsProtocol.SIXEL:
                kept.append(p)
                continue
            pr0, pr1 = p.abs_row, p.abs_row + p.rows
            pc0, pc1 = p.col, p.col + p.cols
            if r1 <= pr0 or r0 >= pr1 or c1 <= pc0 or c0 >= pc1:
                kept.append(p)
                continue
            if policy == "whole_image" or p.rows * p.cols > _MAX_MASK_CELLS:
                changed = True
                continue
            prev_mask = p.visible_mask
            if prev_mask is None:
                prev_mask = (1 << (p.rows * p.cols)) - 1
            mask = prev_mask
            ir0 = max(r0, pr0) - pr0
            ir1 = min(r1, pr1) - pr0
            ic0 = max(c0, pc0) - pc0
            ic1 = min(c1, pc1) - pc0
            if ic0 == 0 and ic1 == p.cols:
                span = (ir1 - ir0) * p.cols
                mask &= ~(((1 << span) - 1) << (ir0 * p.cols))
            else:
                row_clear = ((1 << (ic1 - ic0)) - 1) << ic0
                for rr in range(ir0, ir1):
                    mask &= ~(row_clear << (rr * p.cols))
            if mask == 0:
                changed = True
                continue
            if mask != prev_mask:
                p.visible_mask = mask
                _trim_masked_sixel_rows(p)
                changed = True
                if p.rows <= 0:
                    continue
            kept.append(p)
        if changed:
            if len(kept) != len(self._placements):
                self._placements = kept
                self._prune_orphans()
            self._bump()

    def on_region_scroll(
        self,
        *,
        top: int,
        bottom: int,
        left: int,
        right: int,
        delta_rows: int,
        is_scroll: bool,
        scrolled_total: int,
        cell_height_px: int,
        history_advanced: int = 0,
    ) -> None:
        """Adjust placements for bounded margin scroll or ``IL``/``DL``."""
        if not self._placements or delta_rows == 0:
            return
        abs_top = scrolled_total + top
        abs_bot = scrolled_total + bottom
        changed = False
        kept: list[_MutablePlacement] = []

        for p in self._placements:
            if p.virtual:
                kept.append(p)
                continue
            p_top = p.abs_row
            p_bot = p.abs_row + p.rows - 1
            p_left = p.col
            p_right = p.col + p.cols - 1

            if history_advanced > 0:
                if p_bot <= abs_bot:
                    kept.append(p)
                    continue
                if p_top > abs_bot:
                    p.abs_row += history_advanced
                    changed = True
                    kept.append(p)
                    continue

            inside = (
                p_top >= abs_top
                and p_bot <= abs_bot
                and p_left >= left
                and p_right <= right
            )

            if p.protocol == GraphicsProtocol.KITTY or p.parent_image_id > 0:
                if not is_scroll or not inside:
                    if history_advanced > 0 and p_top >= scrolled_total:
                        p.abs_row += history_advanced
                        changed = True
                    kept.append(p)
                    continue
                new_top = p_top + delta_rows + history_advanced
                new_bot = p_bot + delta_rows + history_advanced
                eff_top = abs_top + history_advanced
                eff_bot = abs_bot + history_advanced
                clip_top = max(0, eff_top - new_top)
                clip_bot = max(0, new_bot - eff_bot)
                if clip_top + clip_bot >= p.rows:
                    changed = True
                    continue
                if clip_top > 0:
                    crop_px = (
                        (p.source_height * clip_top) // p.rows
                        if p.rows > 0
                        else cell_height_px * clip_top
                    )
                    p.source_y += crop_px
                    p.source_height = max(0, p.source_height - crop_px)
                    p.rows -= clip_top
                    new_top = eff_top
                    if p.source_height <= 0 or p.rows <= 0:
                        changed = True
                        continue
                if clip_bot > 0:
                    crop_px = (
                        (p.source_height * clip_bot) // p.rows
                        if p.rows > 0
                        else cell_height_px * clip_bot
                    )
                    p.source_height = max(0, p.source_height - crop_px)
                    p.rows -= clip_bot
                    if p.source_height <= 0 or p.rows <= 0:
                        changed = True
                        continue
                p.abs_row = new_top
                changed = True
                kept.append(p)
            else:
                if (
                    p_bot < abs_top
                    or p_top > abs_bot
                    or p_right < left
                    or p_left > right
                ):
                    if history_advanced > 0 and p_top > abs_bot:
                        p.abs_row += history_advanced
                        changed = True
                    kept.append(p)
                    continue
                if not inside:
                    anchored_below = history_advanced > 0 and p_top >= scrolled_total
                    if anchored_below:
                        p.abs_row += history_advanced
                        changed = True
                    if p.rows * p.cols <= _MAX_MASK_CELLS:
                        prev_mask = (
                            (1 << (p.rows * p.cols)) - 1
                            if p.visible_mask is None
                            else p.visible_mask
                        )
                        if history_advanced > 0 and not anchored_below:
                            ir0 = max(0, abs_bot + 1 - p_top)
                            ir1 = p.rows
                        else:
                            ir0 = max(p_top, abs_top) - p_top
                            ir1 = min(p_bot, abs_bot) - p_top + 1
                        ic0 = max(p_left, left) - p_left
                        ic1 = min(p_right, right) - p_left + 1
                        if ir0 < ir1 and ic0 < ic1:
                            row_clear = ((1 << (ic1 - ic0)) - 1) << ic0
                            mask = prev_mask
                            for rr in range(ir0, ir1):
                                mask &= ~(row_clear << (rr * p.cols))
                            if mask == 0:
                                changed = True
                                continue
                            if mask != prev_mask:
                                p.visible_mask = mask
                                _trim_masked_sixel_rows(p)
                                changed = True
                                if p.rows <= 0:
                                    continue
                    kept.append(p)
                    continue

                eff_top = abs_top + history_advanced
                eff_bot = abs_bot + history_advanced
                new_top = p_top + delta_rows + history_advanced
                new_bot = p_bot + delta_rows + history_advanced
                clip_top = max(0, eff_top - new_top)
                clip_bot = max(0, new_bot - eff_bot)
                if clip_top + clip_bot >= p.rows:
                    changed = True
                    continue
                keep_rows = p.rows - clip_top - clip_bot
                if (
                    p.visible_mask is not None
                    and (clip_top > 0 or clip_bot > 0)
                    and keep_rows * p.cols <= _MAX_MASK_CELLS
                ):
                    shifted = p.visible_mask >> (clip_top * p.cols)
                    p.visible_mask = shifted & ((1 << (keep_rows * p.cols)) - 1)
                    if p.visible_mask == 0:
                        changed = True
                        continue
                p.rows = keep_rows
                p.abs_row = max(eff_top, new_top)
                changed = True
                kept.append(p)

        if changed:
            self._placements = kept
            self._prune_orphans()
            self._bump()

    def prepare_sixel(
        self,
        payload: bytes,
        *,
        col: int,
        screen_cols: int,
        cell_width_px: int,
        cell_height_px: int,
    ) -> tuple[int, int, int, int] | None:
        """Scan ``SIXEL`` geometry into ``(width_px, height_px, rows, cols)``."""
        w_px, h_px = scan_sixel_geometry(payload, max_dimension=self._max_dim)
        if w_px <= 0 or h_px <= 0:
            return None
        cw = max(1, cell_width_px)
        ch = max(1, cell_height_px)
        cols = min(max(1, screen_cols - col), max(1, (w_px + cw - 1) // cw))
        rows = max(1, (h_px + ch - 1) // ch)
        return w_px, h_px, rows, cols

    def commit_sixel(
        self,
        *,
        payload_len: int,
        width_px: int,
        height_px: int,
        row: int,
        col: int,
        rows: int,
        cols: int,
        scrolled_total: int,
        min_abs: int = 0,
    ) -> None:
        """Record a scanned ``SIXEL`` image and placement at post-scroll *row*."""
        abs_r = scrolled_total + row
        eff_rows = rows
        eff_h_px = height_px
        eff_src_y = 0
        if abs_r < min_abs:
            clip = min_abs - abs_r
            if clip >= eff_rows:
                return
            if eff_h_px > 0 and eff_rows > 0:
                crop_px = (eff_h_px * clip) // eff_rows
                eff_src_y = crop_px
                eff_h_px = max(1, eff_h_px - crop_px)
            eff_rows -= clip
            abs_r = min_abs
        img_id = self._next_auto_id
        self._next_auto_id += 1
        meta = ImageMeta(
            protocol=GraphicsProtocol.SIXEL,
            image_id=img_id,
            width_px=width_px,
            height_px=height_px,
            format_code=0,
            byte_len=payload_len,
        )
        self._store_image(meta)
        self._append_placement(
            _MutablePlacement(
                protocol=GraphicsProtocol.SIXEL,
                image_id=img_id,
                placement_id=0,
                abs_row=max(0, abs_r),
                col=col,
                rows=eff_rows,
                cols=cols,
                source_y=eff_src_y,
                source_width=width_px,
                source_height=eff_h_px,
            ),
        )

    def _drain_decompressor(self, up: _PendingKittyUpload, data: bytes) -> None:
        dec = up.decompressor
        if dec is None:
            need = 24 - len(up.header_buf)
            if need > 0:
                up.header_buf.extend(data[:need])
            return
        buf = data
        while buf and up.decompressed_bytes < up.inflate_cap:
            step = min(_ZLIB_STEP_BYTES, up.inflate_cap - up.decompressed_bytes)
            try:
                out = dec.decompress(buf, step)
            except zlib.error:
                up.failed_error = "EINVAL:invalid zlib stream"
                return
            if not out and not dec.eof:
                break
            need = 24 - len(up.header_buf)
            if need > 0:
                up.header_buf.extend(out[:need])
            up.decompressed_bytes += len(out)
            buf = dec.unconsumed_tail
            if dec.eof:
                break

    def _feed_upload_chunk(self, up: _PendingKittyUpload, payload: bytes) -> None:
        if up.failed_error is not None:
            return
        if up.encoded_bytes + len(payload) > _MAX_UPLOAD_BYTES:
            up.failed_error = "ENOMEM:upload exceeds byte limit"
            return
        cleaned = b"".join(payload.split())
        if not cleaned:
            return
        up.encoded_bytes += len(cleaned)
        combined = up.b64_rem + cleaned
        rem_len = len(combined) % 4
        if rem_len:
            decode_part = combined[:-rem_len]
            up.b64_rem = combined[-rem_len:]
        else:
            decode_part = combined
            up.b64_rem = b""
        if not decode_part:
            return
        try:
            raw = base64.b64decode(decode_part, validate=True)
        except binascii.Error:
            up.failed_error = "EINVAL:invalid base64 payload"
            return
        up.decoded_bytes += len(raw)
        self._drain_decompressor(up, raw)

    def _finalize_upload(self, up: _PendingKittyUpload) -> None:
        if up.failed_error is not None:
            return
        if up.b64_rem:
            padded = up.b64_rem + b"=" * ((4 - len(up.b64_rem)) % 4)
            try:
                raw = base64.b64decode(padded, validate=True)
            except binascii.Error:
                up.failed_error = "EINVAL:invalid base64 tail"
                return
            up.decoded_bytes += len(raw)
            self._drain_decompressor(up, raw)
            if up.failed_error is not None:
                return
        if up.decompressor is not None and up.decompressed_bytes < up.inflate_cap:
            try:
                tail = up.decompressor.flush(_ZLIB_STEP_BYTES)
            except zlib.error:
                up.failed_error = "EINVAL:invalid zlib stream"
                return
            need = 24 - len(up.header_buf)
            if need > 0:
                up.header_buf.extend(tail[:need])
            up.decompressed_bytes += len(tail)
            if not up.decompressor.eof and up.decompressed_bytes < up.inflate_cap:
                up.failed_error = "EINVAL:truncated zlib stream"

    def handle_kitty(
        self,
        control: dict[str, str],
        payload: bytes,
        *,
        cursor_row: int,
        cursor_col: int,
        screen_rows: int,
        screen_cols: int,
        scrolled_total_getter: collections.abc.Callable[[], int] | None = None,
        scrolled_total: int = 0,
        cell_width_px: int = 8,
        cell_height_px: int = 16,
        min_row: int = 0,
        advance_cursor: collections.abc.Callable[[int, int], int] | None = None,
    ) -> bytes | None:
        """Process one Kitty ``APC G`` packet and return optional reply bytes.

        Parameters
        ----------
        control : dict[str, str]
            Parsed ``key=value`` control header pairs.
        payload : bytes
            Base64 payload segment following ``;``.
        cursor_row : int
            Zero-based cursor row at packet arrival.
        cursor_col : int
            Zero-based cursor column at packet arrival.
        screen_rows : int
            Active screen height in rows.
        screen_cols : int
            Active screen width in columns.
        scrolled_total_getter : collections.abc.Callable[[], int] | None
            Optional callback returning live ``scrolled_total`` after
            *advance_cursor* runs.
        scrolled_total : int
            Fallback ``scrolled_total`` when *scrolled_total_getter* is ``None``.
        cell_width_px : int
            Nominal cell width in pixels.
        cell_height_px : int
            Nominal cell height in pixels.
        min_row : int
            Minimum viewport row ceiling when cursor scrolling does not advance
            scrollback (``0`` default, or active ``DECSTBM`` top margin).
        advance_cursor : collections.abc.Callable[[int, int], int] | None
            Callback invoked with ``(rows, cols)`` prior to committing a
            cursor-moving ``C=0`` placement, returning the post-scroll anchor
            row.

        Returns
        -------
        bytes | None
            Sans-I/O ``APC G`` response packet, or ``None`` when silent.
        """
        explicit_action = control.get("a")
        more = _parse_int(control, "m", 0) == 1
        cur_scrolled = (
            scrolled_total_getter()
            if scrolled_total_getter is not None
            else scrolled_total
        )

        if explicit_action == "d":
            self._pending = None
            for id_key in ("i", "I", "p", "P", "Q"):
                raw_id = control.get(id_key)
                if (
                    raw_id is not None
                    and raw_id != ""
                    and _parse_ascii_uint(raw_id, max_val=_KITTY_UINT32_MAX) is None
                ):
                    return None
            if (
                _parse_uint32(control, "i", 0) > 0
                and _parse_uint32(control, "I", 0) > 0
            ):
                return None
            self._handle_kitty_delete(
                control,
                cursor_row=cursor_row,
                cursor_col=cursor_col,
                screen_rows=screen_rows,
                scrolled_total=cur_scrolled,
            )
            return None

        reply_ctx = control
        if self._pending is not None:
            up = self._pending
            raw_in_i = control.get("i")
            in_i_valid = (
                _parse_ascii_uint(raw_in_i, max_val=_KITTY_UINT32_MAX)
                if (raw_in_i is not None and raw_in_i != "")
                else 0
            )
            up_id = _parse_uint32(up.control, "i", 0)
            stale = (
                explicit_action in ("q", "p")
                or ("t" in control and control.get("t", "d") != "d")
                or (
                    in_i_valid is not None
                    and in_i_valid > 0
                    and up_id > 0
                    and in_i_valid != up_id
                )
            )
            if stale:
                self._pending = None
            elif explicit_action in (None, "t", "T"):
                merged_q = max(_valid_quiet(up.control), _valid_quiet(control))
                merged_ctrl = dict(up.control)
                for k, v in control.items():
                    if k not in ("m", "q"):
                        merged_ctrl.setdefault(k, v)
                if merged_q > 0:
                    merged_ctrl["q"] = str(merged_q)
                reply_ctx = merged_ctrl

                for id_key in ("i", "I", "p", "P", "Q"):
                    raw_id = control.get(id_key)
                    if (
                        raw_id is not None
                        and raw_id != ""
                        and _parse_ascii_uint(raw_id, max_val=_KITTY_UINT32_MAX) is None
                    ):
                        self._pending = None
                        return _kitty_reply(
                            reply_ctx,
                            f"EINVAL:invalid uint32 {id_key}",
                            is_error=True,
                        )
                if (
                    _parse_uint32(merged_ctrl, "i", 0) > 0
                    and _parse_uint32(merged_ctrl, "I", 0) > 0
                ):
                    self._pending = None
                    return _kitty_reply(
                        reply_ctx,
                        "EINVAL:cannot specify both i and I",
                        is_error=True,
                    )

                up.control = merged_ctrl
                up.chunks += 1
                if up.chunks > _MAX_UPLOAD_CHUNKS:
                    self._pending = None
                    return _kitty_reply(
                        up.control, "ENOMEM:too many chunks", is_error=True
                    )
                self._feed_upload_chunk(up, payload)
                if more:
                    return None
                self._pending = None
                self._finalize_upload(up)
                return self._complete_kitty_upload(
                    up,
                    cursor_row=cursor_row,
                    cursor_col=cursor_col,
                    screen_cols=screen_cols,
                    scrolled_total_getter=scrolled_total_getter,
                    scrolled_total=cur_scrolled,
                    cell_width_px=cell_width_px,
                    cell_height_px=cell_height_px,
                    min_row=min_row,
                    advance_cursor=advance_cursor,
                )

        for id_key in ("i", "I", "p", "P", "Q"):
            raw_id = control.get(id_key)
            if (
                raw_id is not None
                and raw_id != ""
                and _parse_ascii_uint(raw_id, max_val=_KITTY_UINT32_MAX) is None
            ):
                self._pending = None
                return _kitty_reply(
                    reply_ctx,
                    f"EINVAL:invalid uint32 {id_key}",
                    is_error=True,
                )

        if _parse_uint32(control, "i", 0) > 0 and _parse_uint32(control, "I", 0) > 0:
            self._pending = None
            return _kitty_reply(
                reply_ctx, "EINVAL:cannot specify both i and I", is_error=True
            )

        action = explicit_action or "t"
        if action in ("t", "T", "q"):
            if action == "q" and _parse_uint32(control, "i", 0) <= 0:
                if _parse_uint32(control, "I", 0) > 0:
                    return _kitty_reply(
                        control, "EINVAL:image ID required for query", is_error=True
                    )
                return None
            medium = control.get("t", "d")
            if medium != "d":
                self._pending = None
                return _kitty_reply(
                    control,
                    f"EINVAL:unsupported transmission medium {medium!a}",
                    is_error=True,
                )
            comp = control.get("o", "")
            if comp not in ("", "z"):
                return _kitty_reply(
                    control,
                    f"EINVAL:unsupported compression {comp!a}",
                    is_error=True,
                )
            fmt = _parse_int(control, "f", 32)
            if fmt == 100:
                inflate_cap = _MAX_ZLIB_INFLATE_BYTES
            else:
                w_decl = min(self._max_dim + 1, max(0, _parse_int(control, "s", 0)))
                h_decl = min(self._max_dim + 1, max(0, _parse_int(control, "v", 0)))
                bpp = max(1, fmt // 8)
                inflate_cap = min(
                    _MAX_ZLIB_INFLATE_BYTES, max(24, w_decl * h_decl * bpp)
                )
            up = _PendingKittyUpload(
                control=dict(control),
                b64_rem=b"",
                header_buf=bytearray(),
                decompressor=zlib.decompressobj() if comp == "z" else None,
                inflate_cap=inflate_cap,
                encoded_bytes=0,
                decoded_bytes=0,
                decompressed_bytes=0,
                chunks=1,
            )
            self._feed_upload_chunk(up, payload)
            if more:
                self._pending = up
                return None
            self._finalize_upload(up)
            return self._complete_kitty_upload(
                up,
                cursor_row=cursor_row,
                cursor_col=cursor_col,
                screen_cols=screen_cols,
                scrolled_total_getter=scrolled_total_getter,
                scrolled_total=cur_scrolled,
                cell_width_px=cell_width_px,
                cell_height_px=cell_height_px,
                min_row=min_row,
                advance_cursor=advance_cursor,
            )

        if action == "p":
            return self._place_kitty_image(
                control,
                cursor_row=cursor_row,
                cursor_col=cursor_col,
                screen_cols=screen_cols,
                scrolled_total_getter=scrolled_total_getter,
                scrolled_total=cur_scrolled,
                cell_width_px=cell_width_px,
                cell_height_px=cell_height_px,
                min_row=min_row,
                advance_cursor=advance_cursor,
            )

        return None

    def _complete_kitty_upload(
        self,
        up: _PendingKittyUpload,
        *,
        cursor_row: int,
        cursor_col: int,
        screen_cols: int,
        scrolled_total_getter: collections.abc.Callable[[], int] | None,
        scrolled_total: int,
        cell_width_px: int,
        cell_height_px: int,
        min_row: int = 0,
        advance_cursor: collections.abc.Callable[[int, int], int] | None,
    ) -> bytes | None:
        ctrl = up.control
        if up.failed_error is not None:
            return _kitty_reply(ctrl, up.failed_error, is_error=True)

        fmt = _parse_int(ctrl, "f", 32)
        if fmt == 100:
            dims = extract_png_dimensions(bytes(up.header_buf))
            if dims is None:
                return _kitty_reply(ctrl, "EINVAL:invalid PNG header", is_error=True)
            w_px, h_px = dims
        elif fmt in (8, 16, 24, 32):
            w_px = _parse_int(ctrl, "s", 0)
            h_px = _parse_int(ctrl, "v", 0)
            if w_px <= 0 or h_px <= 0:
                return _kitty_reply(
                    ctrl, "EINVAL:missing raw s/v dimensions", is_error=True
                )
            if w_px > self._max_dim or h_px > self._max_dim:
                return _kitty_reply(
                    ctrl, "EINVAL:image dimensions exceed limit", is_error=True
                )
            expected = w_px * h_px * (fmt // 8)
            if ctrl.get("o") == "z":
                req = min(expected, _MAX_ZLIB_INFLATE_BYTES)
                if up.decompressed_bytes < req:
                    return _kitty_reply(
                        ctrl, "EINVAL:truncated compressed payload", is_error=True
                    )
            elif up.decoded_bytes < expected:
                return _kitty_reply(ctrl, "EINVAL:truncated raw payload", is_error=True)
        else:
            return _kitty_reply(
                ctrl, f"EINVAL:unsupported format f={fmt}", is_error=True
            )

        if w_px > self._max_dim or h_px > self._max_dim:
            return _kitty_reply(
                ctrl, "EINVAL:image dimensions exceed limit", is_error=True
            )

        action = ctrl.get("a", "t")
        if action == "q":
            return _kitty_reply(ctrl, "OK", is_error=False)

        client_id = _parse_int(ctrl, "i", 0)
        client_num = _parse_int(ctrl, "I", 0)
        if client_id > 0:
            img_id = client_id
        else:
            img_id = self._next_auto_id
            self._next_auto_id += 1
        meta = ImageMeta(
            protocol=GraphicsProtocol.KITTY,
            image_id=img_id,
            image_number=client_num,
            width_px=w_px,
            height_px=h_px,
            format_code=fmt,
            compressed=ctrl.get("o") == "z",
            byte_len=up.encoded_bytes,
        )
        self._store_image(meta)
        reply_id = img_id if (client_id > 0 or client_num > 0) else None
        if action == "T":
            return self._place_kitty_image(
                ctrl,
                cursor_row=cursor_row,
                cursor_col=cursor_col,
                screen_cols=screen_cols,
                scrolled_total_getter=scrolled_total_getter,
                scrolled_total=scrolled_total,
                cell_width_px=cell_width_px,
                cell_height_px=cell_height_px,
                min_row=min_row,
                advance_cursor=advance_cursor,
                target_meta=meta,
                reply_image_id=reply_id,
            )
        return _kitty_reply(ctrl, "OK", is_error=False, reply_image_id=reply_id)

    def _resolve_kitty_image(self, control: dict[str, str]) -> ImageMeta | None:
        img_id = _parse_int(control, "i", 0)
        if img_id > 0:
            meta = self._images.get(img_id)
            return (
                meta
                if meta is not None and meta.protocol == GraphicsProtocol.KITTY
                else None
            )
        img_num = _parse_int(control, "I", 0)
        if img_num > 0:
            for meta in reversed(self._images.values()):
                if (
                    meta.protocol == GraphicsProtocol.KITTY
                    and meta.image_number == img_num
                ):
                    return meta
        return None

    def _find_parent_placement(
        self,
        parent_id: int,
        parent_pid: int,
    ) -> _MutablePlacement | None:
        for p in reversed(self._placements):
            if (
                p.protocol == GraphicsProtocol.KITTY
                and p.image_id == parent_id
                and (parent_pid == 0 or p.placement_id == parent_pid)
            ):
                return p
        return None

    def _validate_parent_chain(
        self,
        child_img_id: int,
        child_pid: int,
        parent: _MutablePlacement,
    ) -> str | None:
        if (
            child_img_id == parent.image_id
            and child_pid == parent.placement_id
            and (child_pid > 0 or parent.placement_id == 0)
        ):
            return "ECYCLE:self-parented placement"
        cur: _MutablePlacement | None = parent
        depth = 1
        while cur is not None and cur.parent_image_id > 0:
            if depth >= _MAX_PARENT_DEPTH:
                return "ETOODEEP:relative placement chain exceeds limit"
            if cur.parent_image_id == child_img_id and (
                cur.parent_placement_id in (0, child_pid)
            ):
                return "ECYCLE:cyclic relative placement"
            cur = self._find_parent_placement(
                cur.parent_image_id, cur.parent_placement_id
            )
            depth += 1
        return None

    def _place_kitty_image(
        self,
        control: dict[str, str],
        *,
        cursor_row: int,
        cursor_col: int,
        screen_cols: int,
        scrolled_total_getter: collections.abc.Callable[[], int] | None,
        scrolled_total: int,
        cell_width_px: int,
        cell_height_px: int,
        min_row: int = 0,
        advance_cursor: collections.abc.Callable[[int, int], int] | None,
        target_meta: ImageMeta | None = None,
        reply_image_id: int | None = None,
    ) -> bytes | None:
        meta = (
            target_meta
            if target_meta is not None
            else self._resolve_kitty_image(control)
        )
        if meta is None:
            return _kitty_reply(control, "ENOENT:image not found", is_error=True)

        virtual = _parse_int(control, "U", 0) == 1
        parent_id = _parse_int(control, "P", 0)
        parent_pid = _parse_int(control, "Q", 0)
        pid = _parse_int(control, "p", 0)
        if virtual and parent_id > 0:
            return _kitty_reply(
                control,
                "EINVAL:virtual placement cannot be relative",
                is_error=True,
            )

        parent_pl: _MutablePlacement | None = None
        if parent_id > 0:
            parent_meta = self._images.get(parent_id)
            if parent_meta is None or parent_meta.protocol != GraphicsProtocol.KITTY:
                return _kitty_reply(
                    control, "ENOPARENT:parent image not found", is_error=True
                )
            parent_pl = self._find_parent_placement(parent_id, parent_pid)
            if parent_pl is None or parent_pl.virtual:
                return _kitty_reply(
                    control, "ENOPARENT:parent placement not found", is_error=True
                )
            chain_err = self._validate_parent_chain(meta.image_id, pid, parent_pl)
            if chain_err is not None:
                return _kitty_reply(control, chain_err, is_error=True)

        cw = max(1, cell_width_px)
        ch = max(1, cell_height_px)
        src_x = min(meta.width_px, max(0, _parse_int(control, "x", 0)))
        src_y = min(meta.height_px, max(0, _parse_int(control, "y", 0)))
        req_w = max(0, _parse_int(control, "w", 0))
        req_h = max(0, _parse_int(control, "h", 0))
        src_w = (
            min(req_w, meta.width_px - src_x) if req_w > 0 else (meta.width_px - src_x)
        )
        src_h = (
            min(req_h, meta.height_px - src_y)
            if req_h > 0
            else (meta.height_px - src_y)
        )
        off_x = min(cw - 1, max(0, _parse_int(control, "X", 0)))
        off_y = min(ch - 1, max(0, _parse_int(control, "Y", 0)))
        req_c = max(0, _parse_int(control, "c", 0))
        req_r = max(0, _parse_int(control, "r", 0))

        if req_c > 0 and req_r > 0:
            cols, rows = req_c, req_r
        elif req_c > 0:
            cols = req_c
            w_px = max(1, req_c * cw - off_x)
            h_px = max(1, (w_px * max(1, src_h) + max(1, src_w) - 1) // max(1, src_w))
            rows = max(1, (h_px + off_y + ch - 1) // ch)
        elif req_r > 0:
            rows = req_r
            h_px = max(1, req_r * ch - off_y)
            w_px = max(1, (h_px * max(1, src_w) + max(1, src_h) - 1) // max(1, src_h))
            cols = max(1, (w_px + off_x + cw - 1) // cw)
        else:
            cols = max(1, (max(1, src_w) + off_x + cw - 1) // cw)
            rows = max(1, (max(1, src_h) + off_y + ch - 1) // ch)

        cols = min(max(1, screen_cols * 4), cols)
        rows = min(max(1, self._max_dim), rows)
        z_idx = max(
            _KITTY_INT32_MIN, min(_KITTY_INT32_MAX, _parse_int(control, "z", 0))
        )
        move_cursor = (
            not virtual and parent_id == 0 and _parse_int(control, "C", 0) == 0
        )

        eff_reply_id = (
            reply_image_id
            if target_meta is not None
            else (
                meta.image_id
                if (
                    _parse_uint32(control, "i", 0) > 0
                    or _parse_uint32(control, "I", 0) > 0
                )
                else None
            )
        )

        if parent_pl is not None:
            rel_v = max(
                -self._max_dim,
                min(self._max_dim, _parse_int(control, "V", 0)),
            )
            rel_h = max(
                -self._max_dim,
                min(self._max_dim, _parse_int(control, "H", 0)),
            )
            abs_r = parent_pl.abs_row + rel_v
            col_pos = max(0, min(screen_cols - 1, parent_pl.col + rel_h))
            min_abs = 0
        else:
            anchor_r = cursor_row
            if move_cursor and advance_cursor is not None:
                anchor_r = advance_cursor(rows, cols)
            post_scrolled = (
                scrolled_total_getter()
                if scrolled_total_getter is not None
                else scrolled_total
            )
            abs_r = post_scrolled + anchor_r
            col_pos = cursor_col
            min_abs = (
                (post_scrolled + min_row) if post_scrolled == scrolled_total else 0
            )

        if not virtual and abs_r < min_abs:
            clip_top = min_abs - abs_r
            if clip_top >= rows:
                return _kitty_reply(
                    control, "OK", is_error=False, reply_image_id=eff_reply_id
                )
            if src_h > 0 and rows > 0:
                crop_px = (src_h * clip_top) // rows
                src_y += crop_px
                src_h = max(1, src_h - crop_px)
            rows -= clip_top
            abs_r = min_abs

        self._append_placement(
            _MutablePlacement(
                protocol=GraphicsProtocol.KITTY,
                image_id=meta.image_id,
                placement_id=pid,
                abs_row=max(0, abs_r),
                col=col_pos,
                rows=rows,
                cols=cols,
                z_index=z_idx,
                virtual=virtual,
                parent_image_id=parent_id,
                parent_placement_id=parent_pid,
                source_x=src_x,
                source_y=src_y,
                source_width=src_w,
                source_height=src_h,
                cell_offset_x=off_x,
                cell_offset_y=off_y,
            ),
        )
        return _kitty_reply(control, "OK", is_error=False, reply_image_id=eff_reply_id)

    def _prune_orphans(self, *, free_images: bool = False) -> None:
        if not self._placements:
            return
        children_by_key: dict[tuple[int, int], list[int]] = {}
        children_by_img: dict[int, list[int]] = {}
        reachable: list[bool] = [False] * len(self._placements)
        queue: collections.deque[int] = collections.deque()

        for idx, p in enumerate(self._placements):
            if p.parent_image_id <= 0:
                reachable[idx] = True
                if not p.virtual:
                    queue.append(idx)
            elif p.parent_placement_id > 0:
                children_by_key.setdefault(
                    (p.parent_image_id, p.parent_placement_id), []
                ).append(idx)
            else:
                children_by_img.setdefault(p.parent_image_id, []).append(idx)

        if len(queue) == len(self._placements):
            return

        while queue:
            cur_idx = queue.popleft()
            cur_p = self._placements[cur_idx]
            for child_idx in children_by_key.get(
                (cur_p.image_id, cur_p.placement_id), ()
            ):
                if not reachable[child_idx]:
                    reachable[child_idx] = True
                    queue.append(child_idx)
            for child_idx in children_by_img.get(cur_p.image_id, ()):
                if not reachable[child_idx]:
                    reachable[child_idx] = True
                    queue.append(child_idx)

        kept: list[_MutablePlacement] = []
        removed_ids: set[int] = set()
        for idx, p in enumerate(self._placements):
            if reachable[idx]:
                kept.append(p)
            else:
                removed_ids.add(p.image_id)
        self._placements = kept
        if free_images and removed_ids:
            still_used = {p.image_id for p in self._placements}
            for rid in removed_ids - still_used:
                self._images.pop(rid, None)

    def _handle_kitty_delete(
        self,
        control: dict[str, str],
        *,
        cursor_row: int,
        cursor_col: int,
        screen_rows: int,
        scrolled_total: int,
    ) -> None:
        raw_sel = control.get("d", "a") or "a"
        sel = raw_sel[0]
        free_images = sel.isupper()
        kind = sel.lower()
        pid_filter = _parse_int(control, "p", 0)
        top_abs = scrolled_total
        bot_abs = scrolled_total + screen_rows

        candidates: set[int] = set()
        kept: list[_MutablePlacement] = []

        if kind == "a":
            for p in self._placements:
                if (
                    p.virtual
                    or p.parent_image_id > 0
                    or p.abs_row + p.rows <= top_abs
                    or p.abs_row >= bot_abs
                ):
                    kept.append(p)
                else:
                    candidates.add(p.image_id)
        elif kind in ("i", "n"):
            target = (
                _parse_int(control, "i", 0)
                if kind == "i"
                else (
                    meta.image_id
                    if (meta := self._resolve_kitty_image(control)) is not None
                    else 0
                )
            )
            if target > 0 and pid_filter == 0:
                candidates.add(target)
            for p in self._placements:
                if p.image_id == target and (
                    pid_filter == 0 or p.placement_id == pid_filter
                ):
                    candidates.add(p.image_id)
                else:
                    kept.append(p)
        elif kind == "r":
            lo = _parse_int(control, "x", 0)
            hi = _parse_int(control, "y", 0)
            for p in self._placements:
                if lo <= p.image_id <= hi:
                    candidates.add(p.image_id)
                else:
                    kept.append(p)
            if free_images:
                for img_id in [k for k in self._images if lo <= k <= hi]:
                    candidates.add(img_id)
        elif kind in ("c", "p", "q", "x", "y", "z"):
            cell_c = cursor_col if kind == "c" else (_parse_int(control, "x", 0) - 1)
            cell_r = cursor_row if kind == "c" else (_parse_int(control, "y", 0) - 1)
            if kind in ("p", "q", "x") and cell_c < 0:
                return
            if kind in ("p", "q", "y") and not (0 <= cell_r < screen_rows):
                return
            abs_cell_r = scrolled_total + cell_r
            z_target = _parse_int(control, "z", 0)
            for p in self._placements:
                if p.virtual:
                    kept.append(p)
                    continue
                if kind == "z":
                    match = p.z_index == z_target
                elif kind in ("c", "p"):
                    match = (
                        p.abs_row <= abs_cell_r < p.abs_row + p.rows
                        and p.col <= cell_c < p.col + p.cols
                    )
                elif kind == "q":
                    match = (
                        p.z_index == z_target
                        and p.abs_row <= abs_cell_r < p.abs_row + p.rows
                        and p.col <= cell_c < p.col + p.cols
                    )
                elif kind == "x":
                    match = (
                        p.abs_row + p.rows > top_abs
                        and p.abs_row < bot_abs
                        and p.col <= cell_c < p.col + p.cols
                    )
                else:  # kind == "y"
                    match = p.abs_row <= abs_cell_r < p.abs_row + p.rows
                if match:
                    candidates.add(p.image_id)
                else:
                    kept.append(p)
        else:
            return

        changed = len(kept) != len(self._placements)
        self._placements = kept
        self._prune_orphans(free_images=free_images)
        if free_images and candidates:
            still_used = {p.image_id for p in self._placements}
            for cid in candidates - still_used:
                if self._images.pop(cid, None) is not None:
                    changed = True
        if changed:
            self._bump()


__all__ = [
    "GraphicsProtocol",
    "GraphicsTracker",
    "ImageMeta",
    "ImagePlacement",
    "ZLayer",
    "extract_png_dimensions",
    "scan_sixel_geometry",
]
