"""Mode-aware input event types, Kitty keyboard flag stacks, and paste guards."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from libvt.exc import UnsafePasteError
from libvt.modes import Mode, ModeState


class KeyMod(enum.IntFlag):
    """Keyboard and mouse modifier bitmask matching Kitty ``CSI u`` bits."""

    NONE = 0
    SHIFT = 1
    ALT = 2
    CTRL = 4
    SUPER = 8
    HYPER = 16
    META = 32


class KeyAction(enum.IntEnum):
    """Key press lifecycle event type for Kitty ``CSI u``."""

    PRESS = 1
    REPEAT = 2
    RELEASE = 3


class KittyKeyboardFlags(enum.IntFlag):
    """Progressive-enhancement flags for the Kitty Keyboard Protocol (``CSI u``)."""

    NONE = 0
    DISAMBIGUATE = 1
    REPORT_EVENT_TYPES = 2
    REPORT_ALTERNATE_KEYS = 4
    REPORT_ALL_KEYS = 8
    REPORT_ASSOCIATED_TEXT = 16


class KittyKeyboardStack:
    """Bounded per-screen stack of :class:`KittyKeyboardFlags`."""

    __slots__ = ("_max_depth", "_stack")

    def __init__(self, *, max_depth: int = 8) -> None:
        self._max_depth = max_depth
        self._stack: list[int] = [0]

    @property
    def flags(self) -> KittyKeyboardFlags:
        """Return currently active :class:`KittyKeyboardFlags`."""
        return KittyKeyboardFlags(self._stack[-1] if self._stack else 0)

    def push(self, flags: int) -> None:
        """Push *flags* onto the stack (``CSI > flags u``), evicting oldest on full."""
        masked = flags & 0x1F
        if len(self._stack) >= self._max_depth:
            self._stack.pop(0)
        self._stack.append(masked)

    def pop(self, count: int = 1) -> None:
        """Pop *count* entries (``CSI < count u``), keeping ``0`` when emptied."""
        n = max(1, count)
        if n >= len(self._stack):
            self._stack = [0]
        else:
            del self._stack[-n:]

    def set_flags(self, flags: int, mode: int = 1) -> None:
        """Update top-of-stack flags via ``CSI = flags ; mode u``."""
        masked = flags & 0x1F
        cur = self._stack[-1] if self._stack else 0
        if mode == 1:
            self._stack[-1] = masked
        elif mode == 2:
            self._stack[-1] = cur | masked
        elif mode == 3:
            self._stack[-1] = cur & ~masked

    def reset(self) -> None:
        """Reset keyboard flags to legacy mode ``0``."""
        self._stack = [0]


_MOD_NAMES: dict[str, KeyMod] = {
    "shift": KeyMod.SHIFT,
    "alt": KeyMod.ALT,
    "opt": KeyMod.ALT,
    "option": KeyMod.ALT,
    "ctrl": KeyMod.CTRL,
    "control": KeyMod.CTRL,
    "super": KeyMod.SUPER,
    "cmd": KeyMod.SUPER,
    "win": KeyMod.SUPER,
    "hyper": KeyMod.HYPER,
    "meta": KeyMod.META,
}


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """Structured keyboard event for outbound PTY encoding.

    Attributes
    ----------
    key : str
        Single Unicode character (e.g. ``"a"``, ``"A"``) or canonical key name
        (``"enter"``, ``"tab"``, ``"escape"``, ``"backspace"``, ``"up"``,
        ``"down"``, ``"right"``, ``"left"``, ``"home"``, ``"end"``,
        ``"page_up"``, ``"page_down"``, ``"insert"``, ``"delete"``,
        ``"f1"`` through ``"f12"``).
    modifiers : KeyMod
        Active modifier bitmask.
    action : KeyAction
        Key action (``PRESS``, ``REPEAT``, or ``RELEASE``).
    text : str
        Optional associated text for Kitty flag ``REPORT_ASSOCIATED_TEXT``.
    """

    key: str
    modifiers: KeyMod = KeyMod.NONE
    action: KeyAction = KeyAction.PRESS
    text: str = ""

    @classmethod
    def parse(cls, chord: str, *, action: KeyAction = KeyAction.PRESS) -> KeyEvent:
        """Parse a human-readable chord such as ``"ctrl+shift+p"`` or ``"up"``."""
        if chord == "+":
            return cls(key="+", action=action)
        parts = chord.split("+")
        mods = KeyMod.NONE
        for raw_mod in parts[:-1]:
            m = _MOD_NAMES.get(raw_mod.strip().lower())
            if m is None:
                msg = f"unknown key modifier {raw_mod!r} in chord {chord!r}"
                raise ValueError(msg)
            mods |= m
        raw_key = parts[-1]
        key = raw_key if len(raw_key) == 1 else raw_key.strip().lower()
        return cls(key=key, modifiers=mods, action=action)


class MouseButton(enum.IntEnum):
    """Mouse buttons and scroll wheel directions."""

    LEFT = 0
    MIDDLE = 1
    RIGHT = 2
    NONE = 3
    WHEEL_UP = 64
    WHEEL_DOWN = 65


class MouseAction(enum.StrEnum):
    """Mouse pointer action."""

    PRESS = "press"
    RELEASE = "release"
    MOVE = "move"


@dataclass(frozen=True, slots=True)
class MouseEvent:
    """Zero-based ``(row, col)`` mouse event for outbound SGR / X10 encoding.

    Attributes
    ----------
    button : MouseButton
        Mouse button or scroll wheel.
    action : MouseAction
        Pointer action (``press``, ``release``, or ``move``).
    row : int
        Zero-based terminal row.
    col : int
        Zero-based terminal column.
    modifiers : KeyMod
        Active keyboard modifiers during the mouse event.
    pixel_x : int
        Horizontal pixel coordinate for ``?1016`` SGR-Pixel mode.
    pixel_y : int
        Vertical pixel coordinate for ``?1016`` SGR-Pixel mode.
    """

    button: MouseButton
    action: MouseAction
    row: int
    col: int
    modifiers: KeyMod = KeyMod.NONE
    pixel_x: int = 0
    pixel_y: int = 0


@dataclass(frozen=True, slots=True)
class FocusEvent:
    """Terminal window focus-in / focus-out event (``?1004``).

    Attributes
    ----------
    focused : bool
        ``True`` on focus gain (``ESC [ I``); ``False`` on focus loss (``ESC [ O``).
    """

    focused: bool


@dataclass(frozen=True, slots=True)
class PasteEvent:
    r"""Clipboard paste payload encoded with ``?2004`` bracketing and safety checks.

    Attributes
    ----------
    text : str
        Text to paste into the terminal.
    sanitize : bool
        When ``False`` (default), raise :exc:`~libvt.exc.UnsafePasteError` on
        control or escape characters. When ``True``, strip control/escape
        codepoints in a single character-level pass.
    allow_unbracketed_newlines : bool
        Permit ``\r`` and ``\n`` when ``?2004`` bracketed paste is disabled.
    """

    text: str
    sanitize: bool = False
    allow_unbracketed_newlines: bool = False


type InputEvent = KeyEvent | MouseEvent | FocusEvent | PasteEvent

_ARROW_CSI: dict[str, str] = {
    "up": "A",
    "down": "B",
    "right": "C",
    "left": "D",
    "home": "H",
    "end": "F",
}

_TILDE_CSI: dict[str, int] = {
    "insert": 2,
    "delete": 3,
    "page_up": 5,
    "page_down": 6,
    "f5": 15,
    "f6": 17,
    "f7": 18,
    "f8": 19,
    "f9": 20,
    "f10": 21,
    "f11": 23,
    "f12": 24,
}

_SS3_FKEYS: dict[str, str] = {
    "f1": "P",
    "f2": "Q",
    "f3": "R",
    "f4": "S",
}

_KITTY_SPECIAL_CP: dict[str, int] = {
    "escape": 27,
    "esc": 27,
    "enter": 13,
    "return": 13,
    "tab": 9,
    "backspace": 127,
}


def is_safe_paste(text: str, *, bracketed: bool = True) -> bool:
    """Return ``True`` when *text* contains no escape or injection control bytes."""
    for ch in text:
        cp = ord(ch)
        if cp == 0x1B or cp == 0x7F or 0x80 <= cp <= 0x9F or cp == 0:
            return False
        if cp < 0x20:
            if ch == "\t":
                continue
            if bracketed and ch in ("\n", "\r"):
                continue
            return False
    return True


def encode_paste(
    text: str,
    *,
    bracketed: bool,
    sanitize: bool = False,
    allow_unbracketed_newlines: bool = False,
) -> bytes:
    """Encode *text* for pasting, failing closed on escape/control injection."""
    if sanitize:
        cleaned: list[str] = []
        for ch in text:
            cp = ord(ch)
            if cp == 0x1B or cp == 0x7F or 0x80 <= cp <= 0x9F or cp == 0:
                continue
            if cp < 0x20:
                if ch == "\t" or (
                    (bracketed or allow_unbracketed_newlines) and ch in ("\n", "\r")
                ):
                    cleaned.append(ch)
                continue
            cleaned.append(ch)
        payload = "".join(cleaned).encode("utf-8")
    else:
        for ch in text:
            cp = ord(ch)
            if cp == 0x1B or cp == 0x7F or 0x80 <= cp <= 0x9F or cp == 0:
                msg = f"paste contains unsafe control/escape codepoint U+{cp:04X}"
                raise UnsafePasteError(msg)
            if cp < 0x20:
                if ch == "\t":
                    continue
                if (bracketed or allow_unbracketed_newlines) and ch in ("\n", "\r"):
                    continue
                msg = (
                    f"paste contains newline/control U+{cp:04X} while bracketed "
                    f"paste (?2004) is {'enabled' if bracketed else 'disabled'}"
                )
                raise UnsafePasteError(msg)
        payload = text.encode("utf-8")

    if bracketed:
        return b"\x1b[200~" + payload + b"\x1b[201~"
    return payload


def _encode_key(
    event: KeyEvent,
    *,
    modes: ModeState,
    kitty_flags: KittyKeyboardFlags,
) -> bytes:
    report_events = bool(kitty_flags & KittyKeyboardFlags.REPORT_EVENT_TYPES)
    if event.action == KeyAction.RELEASE and not report_events:
        return b""

    mod_param = int(event.modifiers) + 1
    ev_suffix = (
        f":{int(event.action)}"
        if report_events and event.action != KeyAction.PRESS
        else ""
    )

    k = event.key
    k_low = k if len(k) == 1 else k.lower()

    # Functional arrows / Home / End
    if k_low in _ARROW_CSI:
        final = _ARROW_CSI[k_low]
        if mod_param == 1 and not ev_suffix:
            if modes.is_set(Mode.DECCKM):
                return f"\x1bO{final}".encode("ascii")
            return f"\x1b[{final}".encode("ascii")
        return f"\x1b[1;{mod_param}{ev_suffix}{final}".encode("ascii")

    # SS3 F1-F4
    if k_low in _SS3_FKEYS:
        final = _SS3_FKEYS[k_low]
        if mod_param == 1 and not ev_suffix:
            return f"\x1bO{final}".encode("ascii")
        return f"\x1b[1;{mod_param}{ev_suffix}{final}".encode("ascii")

    # Tilde functional keys
    if k_low in _TILDE_CSI:
        num = _TILDE_CSI[k_low]
        if mod_param == 1 and not ev_suffix:
            return f"\x1b[{num}~".encode("ascii")
        return f"\x1b[{num};{mod_param}{ev_suffix}~".encode("ascii")

    disambig = bool(
        kitty_flags
        & (KittyKeyboardFlags.DISAMBIGUATE | KittyKeyboardFlags.REPORT_ALL_KEYS),
    )
    report_all = bool(kitty_flags & KittyKeyboardFlags.REPORT_ALL_KEYS)

    # Named C0 keys (escape, enter, tab, backspace)
    if k_low in _KITTY_SPECIAL_CP:
        cp = _KITTY_SPECIAL_CP[k_low]
        if report_all or (
            disambig
            and (
                k_low in ("escape", "esc")
                or event.modifiers != KeyMod.NONE
                or ev_suffix
            )
        ):
            if mod_param == 1 and not ev_suffix:
                return f"\x1b[{cp}u".encode("ascii")
            return f"\x1b[{cp};{mod_param}{ev_suffix}u".encode("ascii")
        if k_low == "tab" and (event.modifiers & KeyMod.SHIFT):
            return b"\x1b[Z"
        raw = bytes([cp])
        return (b"\x1b" + raw) if (event.modifiers & KeyMod.ALT) else raw

    if len(k) != 1:
        msg = f"unsupported key identifier {k!r}"
        raise ValueError(msg)

    cp = ord(k)
    if report_all or (
        disambig
        and ((event.modifiers & ~KeyMod.SHIFT) != KeyMod.NONE or bool(ev_suffix))
    ):
        base_cp = ord(k.lower()) if "A" <= k <= "Z" else cp
        mods = event.modifiers | (KeyMod.SHIFT if "A" <= k <= "Z" else KeyMod.NONE)
        mp = int(mods) + 1
        if mp == 1 and not ev_suffix:
            return f"\x1b[{base_cp}u".encode("ascii")
        return f"\x1b[{base_cp};{mp}{ev_suffix}u".encode("ascii")

    # Legacy xterm key encoding
    out_char = k
    if event.modifiers & KeyMod.CTRL:
        low_cp = ord(k.lower())
        if 0x61 <= low_cp <= 0x7A:
            raw = bytes([low_cp - 0x60])
        elif k in (" ", "@"):
            raw = b"\x00"
        elif k == "[":
            raw = b"\x1b"
        elif k == "\\":
            raw = b"\x1c"
        elif k == "]":
            raw = b"\x1d"
        elif k == "^":
            raw = b"\x1e"
        elif k in ("_", "/"):
            raw = b"\x1f"
        else:
            raw = out_char.encode("utf-8")
    else:
        raw = out_char.encode("utf-8")

    if event.modifiers & KeyMod.ALT:
        return b"\x1b" + raw
    return raw


def _encode_mouse(event: MouseEvent, *, modes: ModeState) -> bytes:
    any_event = modes.is_set(Mode.MOUSE_ANY_EVENT)
    btn_event = modes.is_set(Mode.MOUSE_BUTTON_EVENT)
    vt200 = modes.is_set(Mode.MOUSE_VT200)
    x10 = modes.is_set(Mode.MOUSE_X10)

    if not (any_event or btn_event or vt200 or x10):
        return b""
    if (
        event.action == MouseAction.MOVE
        and not any_event
        and not (btn_event and event.button != MouseButton.NONE)
    ):
        return b""
    if (
        event.action == MouseAction.RELEASE
        and x10
        and not (vt200 or btn_event or any_event)
    ):
        return b""

    code = int(event.button)
    if event.action == MouseAction.MOVE:
        code |= 32
    if event.modifiers & KeyMod.SHIFT:
        code |= 4
    if event.modifiers & KeyMod.ALT:
        code |= 8
    if event.modifiers & KeyMod.CTRL:
        code |= 16

    if modes.is_set(Mode.MOUSE_SGR_PIXELS):
        final = "m" if event.action == MouseAction.RELEASE else "M"
        return f"\x1b[<{code};{event.pixel_x};{event.pixel_y}{final}".encode("ascii")

    if modes.is_set(Mode.MOUSE_SGR):
        final = "m" if event.action == MouseAction.RELEASE else "M"
        return f"\x1b[<{code};{event.col + 1};{event.row + 1}{final}".encode("ascii")

    # Classic X10 / VT200 byte-encoded coordinates (clamped to 223).
    wire_btn = 3 if event.action == MouseAction.RELEASE else code
    cx = min(255, 32 + max(0, event.col) + 1)
    cy = min(255, 32 + max(0, event.row) + 1)
    return bytes([0x1B, ord("["), ord("M"), min(255, 32 + wire_btn), cx, cy])


def encode_input(
    event: InputEvent,
    *,
    modes: ModeState,
    kitty_flags: KittyKeyboardFlags = KittyKeyboardFlags.NONE,
) -> bytes:
    """Encode *event* into outbound PTY bytes respecting active terminal *modes*."""
    if isinstance(event, KeyEvent):
        return _encode_key(event, modes=modes, kitty_flags=kitty_flags)
    if isinstance(event, MouseEvent):
        return _encode_mouse(event, modes=modes)
    if isinstance(event, FocusEvent):
        if not modes.is_set(Mode.FOCUS_EVENT):
            return b""
        return b"\x1b[I" if event.focused else b"\x1b[O"
    return encode_paste(
        event.text,
        bracketed=modes.is_set(Mode.BRACKETED_PASTE),
        sanitize=event.sanitize,
        allow_unbracketed_newlines=event.allow_unbracketed_newlines,
    )


__all__ = [
    "FocusEvent",
    "InputEvent",
    "KeyAction",
    "KeyEvent",
    "KeyMod",
    "KittyKeyboardFlags",
    "KittyKeyboardStack",
    "MouseAction",
    "MouseButton",
    "MouseEvent",
    "PasteEvent",
    "encode_input",
    "encode_paste",
    "is_safe_paste",
]
