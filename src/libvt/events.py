"""Closed algebraic Event union and structured terminal notification records."""

from __future__ import annotations

import enum
import typing as t
from dataclasses import dataclass

if t.TYPE_CHECKING:
    from libvt.config import ColorScheme
    from libvt.status import ProgramStatus


class PromptKind(enum.StrEnum):
    """``OSC 133`` FinalTerm / semantic-shell prompt zone markers."""

    PROMPT_START = "A"
    COMMAND_START = "B"
    COMMAND_EXECUTED = "C"
    COMMAND_FINISHED = "D"


@dataclass(frozen=True, slots=True)
class PromptZone:
    """Semantic shell prompt boundary emitted via ``OSC 133``.

    Attributes
    ----------
    kind : PromptKind
        Marker kind (``A``, ``B``, ``C``, or ``D``).
    row : int
        Zero-based viewport row (negative once scrolled into ``history``).
    col : int
        Zero-based column index when the marker arrived.
    exit_code : int | None
        Parsed exit status on ``D`` markers when supplied, else ``None``.
    abs_row : int
        Monotonic row index from the top of primary scroll history so scrolling
        never invalidates stored coordinates.
    """

    kind: PromptKind
    row: int
    col: int
    exit_code: int | None = None
    abs_row: int = 0


class EraseCause(enum.StrEnum):
    """Reason a screen rectangle was cleared or scrolled."""

    ED = "ed"
    EL = "el"
    ECH = "ech"
    DECSED = "decsed"
    DECSEL = "decsel"
    SCROLL = "scroll"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class Rect:
    """Zero-based rectangular cell region.

    Attributes
    ----------
    row : int
        Top row index (``0``-based).
    col : int
        Left column index (``0``-based).
    rows : int
        Height in rows.
    cols : int
        Width in columns.
    """

    row: int
    col: int
    rows: int
    cols: int


@dataclass(frozen=True, slots=True)
class SixelData:
    """Zero-dependency ``DCS ... q`` SIXEL payload record.

    Attributes
    ----------
    row : int
        Zero-based cursor row at SIXEL entry.
    col : int
        Zero-based cursor column at SIXEL entry.
    params : tuple[int, ...]
        Parsed ``DCS`` numeric header parameters.
    payload : bytes
        Raw SIXEL data bytes up to ``ST``.
    """

    row: int
    col: int
    params: tuple[int, ...]
    payload: bytes


@dataclass(frozen=True, slots=True)
class KittyCommand:
    """Zero-dependency ``APC G`` Kitty graphics protocol record.

    Attributes
    ----------
    row : int
        Zero-based cursor row when the command arrived.
    col : int
        Zero-based cursor column when the command arrived.
    control : dict[str, str]
        Key-value control header pairs before ``;``.
    payload : bytes
        Encoded payload bytes following ``;``.
    """

    row: int
    col: int
    control: dict[str, str]
    payload: bytes


@dataclass(frozen=True, slots=True)
class Bell:
    """Emitted when ``BEL`` (``0x07``) executes in ``GROUND``."""


@dataclass(frozen=True, slots=True)
class TitleChanged:
    """Emitted when ``OSC 0`` or ``OSC 2`` updates the window title.

    Attributes
    ----------
    title : str
        Updated window title.
    """

    title: str


@dataclass(frozen=True, slots=True)
class WorkingDirectoryChanged:
    """Emitted when ``OSC 7`` or ``OSC 9;9`` updates the working directory.

    Attributes
    ----------
    uri : str
        Reported ``file://`` URI or directory path.
    path : str
        Decoded path component extracted from ``uri``.
    """

    uri: str
    path: str


@dataclass(frozen=True, slots=True)
class ClipboardWrite:
    """Emitted when ``OSC 52`` writes base64-decoded text to a selection target.

    Attributes
    ----------
    selection : str
        Clipboard selection targets (e.g. ``"c"`` or ``"p"``).
    text : str
        Decoded UTF-8 text written by the application.
    """

    selection: str
    text: str


@dataclass(frozen=True, slots=True)
class PromptMark:
    """Emitted when ``OSC 133`` records a semantic prompt zone.

    Attributes
    ----------
    zone : PromptZone
        Recorded prompt zone descriptor.
    """

    zone: PromptZone


@dataclass(frozen=True, slots=True)
class ProgramStatusChanged:
    """Emitted when ``OSC 7501`` or ``OSC 9;4`` mutates task status.

    Attributes
    ----------
    status : ProgramStatus | None
        Updated task record, or ``None`` when all records were cleared.
    task_id : str
        Task identifier affected by the update (``""`` for root).
    """

    status: ProgramStatus | None
    task_id: str = ""


@dataclass(frozen=True, slots=True)
class RenderHoldChanged:
    """Emitted when DEC mode ``?2026`` synchronized output starts or ends.

    Attributes
    ----------
    active : bool
        ``True`` on ``?2026h`` start; ``False`` on ``?2026l`` commit or expiry.
    sync_generation : int
        Monotonic synchronized-update batch counter.
    expired : bool
        ``True`` when ended by
        :meth:`~libvt.terminal.Terminal.expire_synchronized_output`.
    """

    active: bool
    sync_generation: int
    expired: bool = False


@dataclass(frozen=True, slots=True)
class ColorSchemeQueried:
    """Emitted when ``CSI ? 996 n`` queries the host color scheme.

    Attributes
    ----------
    scheme : ColorScheme
        Scheme reported back to the application.
    """

    scheme: ColorScheme


@dataclass(frozen=True, slots=True)
class InBandResize:
    """Emitted when ``resize()`` queues a ``?2048`` in-band resize report.

    Attributes
    ----------
    rows : int
        New terminal height in rows.
    cols : int
        New terminal width in columns.
    """

    rows: int
    cols: int


@dataclass(frozen=True, slots=True)
class SixelGraphic:
    """Emitted when a ``DCS ... q`` SIXEL graphic sequence completes.

    Attributes
    ----------
    data : SixelData
        Captured SIXEL header and payload.
    """

    data: SixelData


@dataclass(frozen=True, slots=True)
class KittyGraphic:
    """Emitted when an ``APC G`` Kitty graphics sequence completes.

    Attributes
    ----------
    command : KittyCommand
        Captured Kitty control dictionary and payload.
    """

    command: KittyCommand


@dataclass(frozen=True, slots=True)
class ScreenErased:
    """Emitted when cells clear or scroll on the active screen.

    Attributes
    ----------
    rect : Rect
        Zero-based rectangle affected.
    cause : EraseCause
        Operation that cleared or scrolled the region.
    active_is_alt : bool
        ``True`` when the erase occurred on the ``?1049`` alternate screen.
    """

    rect: Rect
    cause: EraseCause
    active_is_alt: bool


@dataclass(frozen=True, slots=True)
class UnhandledSequence:
    """Recorded when an unrecognized control sequence arrives and ``strict=False``.

    Attributes
    ----------
    kind : str
        Sequence family (``"esc"``, ``"csi"``, ``"osc"``, ``"dcs"``, or ``"apc"``).
    summary : str
        Sanitized compact representation of the unrecognized sequence.
    """

    kind: str
    summary: str


type Event = (
    Bell
    | TitleChanged
    | WorkingDirectoryChanged
    | ClipboardWrite
    | PromptMark
    | ProgramStatusChanged
    | RenderHoldChanged
    | ColorSchemeQueried
    | InBandResize
    | SixelGraphic
    | KittyGraphic
    | ScreenErased
    | UnhandledSequence
)

__all__ = [
    "Bell",
    "ClipboardWrite",
    "ColorSchemeQueried",
    "EraseCause",
    "Event",
    "InBandResize",
    "KittyCommand",
    "KittyGraphic",
    "ProgramStatusChanged",
    "PromptKind",
    "PromptZone",
    "Rect",
    "RenderHoldChanged",
    "ScreenErased",
    "SixelData",
    "SixelGraphic",
    "TitleChanged",
    "UnhandledSequence",
    "WorkingDirectoryChanged",
]
