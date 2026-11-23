"""Rex / tmux / Ghostty ``OSC 7501`` Program Status protocol parser and store."""

from __future__ import annotations

import base64
import binascii
import enum
import re
from dataclasses import dataclass

_APP_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,32}$")
_KEY_RE = re.compile(r"^[a-z]{1,16}$")
_SEG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

MAX_WIRE_BYTES = 4_096
MAX_ID_BYTES = 128
MAX_ID_DEPTH = 8
MAX_TITLE_B64_BYTES = 256
MAX_TITLE_BYTES = 192
MAX_MSG_B64_BYTES = 2_732
MAX_MSG_BYTES = 2_048

_BIDI_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x061C,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    },
)


class StatusState(enum.StrEnum):
    """Foreground task lifecycle states defined by ``OSC 7501``."""

    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    BLOCKED = "blocked"
    ERROR = "error"
    CLEAR = "clear"


class BlockKind(enum.StrEnum):
    """Reason categories when :attr:`StatusState.BLOCKED` requests user action."""

    PERMISSION = "permission"
    QUESTION = "question"
    AUTH = "auth"


_URGENCY_RANK: dict[StatusState, int] = {
    StatusState.BLOCKED: 5,
    StatusState.ERROR: 4,
    StatusState.WORKING: 3,
    StatusState.DONE: 2,
    StatusState.IDLE: 1,
}


@dataclass(frozen=True, slots=True)
class ProgramStatus:
    """Validated ``OSC 7501`` (or ``OSC 9;4`` fallback) task status record.

    Attributes
    ----------
    state : StatusState
        Lifecycle state (``idle``, ``working``, ``done``, ``blocked``, or
        ``error``).
    id : str
        Hierarchical ``/``-delimited task identifier, or ``""`` for the root
        foreground task.
    kind : BlockKind | None
        User-input category when ``state == StatusState.BLOCKED``.
    progress : int | None
        Completion percentage in ``0..100``, when reported.
    app : str | None
        Explicit ``app=`` identifier supplied on this record.
    effective_app : str | None
        ``app`` when present, otherwise inherited from the nearest ancestor
        task prefix in :class:`ProgramStatusStore`.
    title : str | None
        Decoded UTF-8 task title (``<= 192`` UTF-8 bytes).
    msg : str | None
        Decoded UTF-8 detail message (``<= 2048`` UTF-8 bytes).
    """

    state: StatusState
    id: str = ""
    kind: BlockKind | None = None
    progress: int | None = None
    app: str | None = None
    effective_app: str | None = None
    title: str | None = None
    msg: str | None = None

    @property
    def value(self) -> str:
        """Return ``state.value`` string shorthand."""
        return self.state.value


def _validate_id(raw_id: str) -> str | None:
    if not raw_id:
        return ""
    if len(raw_id.encode("utf-8")) > MAX_ID_BYTES:
        return None
    if raw_id.startswith("/") or raw_id.endswith("/"):
        return None
    parts = raw_id.split("/")
    if len(parts) > MAX_ID_DEPTH:
        return None
    if not all(_SEG_RE.match(seg) for seg in parts):
        return None
    return raw_id


def _decode_b64_text(
    raw: str,
    *,
    max_b64: int,
    max_decoded: int,
    allow_tab_newline: bool,
) -> str | None:
    if len(raw) > max_b64:
        return None
    padded = raw + ("=" * ((4 - len(raw) % 4) % 4))
    try:
        decoded_bytes = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded_bytes) > max_decoded:
        return None
    try:
        text = decoded_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    for ch in text:
        cp = ord(ch)
        if cp in _BIDI_CODEPOINTS or 0x7F <= cp <= 0x9F:
            return None
        if cp < 0x20:
            if allow_tab_newline and ch in ("\t", "\n"):
                continue
            return None
    return text


class ProgramStatusStore:
    """Bounded store managing hierarchical ``OSC 7501`` and ``OSC 9;4`` status."""

    __slots__ = ("_max_records", "_native_7501_seen", "_records")

    def __init__(self, *, max_records: int = 64) -> None:
        self._max_records = max_records
        self._records: dict[str, ProgramStatus] = {}
        self._native_7501_seen = False

    @property
    def records(self) -> dict[str, ProgramStatus]:
        """Return a shallow copy of active task records keyed by ``id``."""
        return dict(self._records)

    @property
    def most_urgent(self) -> ProgramStatus | None:
        """Return the highest-urgency active record (preferring root on ties)."""
        if not self._records:
            return None
        return max(
            self._records.values(),
            key=lambda r: (_URGENCY_RANK.get(r.state, 0), r.id == ""),
        )

    def _resolve_effective_app(
        self, task_id: str, explicit_app: str | None
    ) -> str | None:
        if explicit_app is not None:
            return explicit_app
        if not task_id:
            return None
        parts = task_id.split("/")
        for depth in range(len(parts) - 1, 0, -1):
            ancestor = "/".join(parts[:depth])
            rec = self._records.get(ancestor)
            if rec is not None and rec.effective_app is not None:
                return rec.effective_app
        root = self._records.get("")
        return root.effective_app if root is not None else None

    def apply_osc_7501(self, payload: bytes) -> tuple[bool, ProgramStatus | None]:
        """Apply a non-query ``OSC 7501`` byte payload.

        Returns
        -------
        tuple[bool, ProgramStatus | None]
            ``(changed, updated_or_most_urgent)`` indicating whether store
            state changed and the resulting record (or ``None`` after clear).
        """
        if not payload or len(payload) > MAX_WIRE_BYTES:
            return False, self.most_urgent
        try:
            text = payload.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            return False, self.most_urgent

        fields: dict[str, str] = {}
        for token in text.split(":"):
            if not token:
                continue
            eq = token.find("=")
            if eq <= 0:
                return False, self.most_urgent
            key = token[:eq]
            val = token[eq + 1 :]
            if not _KEY_RE.match(key):
                return False, self.most_urgent
            fields[key] = val

        raw_state = fields.get("state")
        if raw_state is None:
            return False, self.most_urgent
        try:
            state = StatusState(raw_state)
        except ValueError:
            return False, self.most_urgent

        task_id = _validate_id(fields.get("id", ""))
        if task_id is None:
            return False, self.most_urgent

        self._native_7501_seen = True

        if state == StatusState.CLEAR:
            if task_id == "":
                if not self._records:
                    return False, None
                self._records.clear()
                return True, None
            prefix = f"{task_id}/"
            to_remove = [
                k for k in self._records if k == task_id or k.startswith(prefix)
            ]
            if not to_remove:
                return False, self.most_urgent
            for k in to_remove:
                del self._records[k]
            return True, self.most_urgent

        kind: BlockKind | None = None
        if "kind" in fields:
            try:
                kind = BlockKind(fields["kind"])
            except ValueError:
                return False, self.most_urgent
            if state != StatusState.BLOCKED:
                kind = None

        progress: int | None = None
        if "progress" in fields:
            raw_prog = fields["progress"]
            if not raw_prog.isdigit():
                return False, self.most_urgent
            prog_val = int(raw_prog)
            if not 0 <= prog_val <= 100:
                return False, self.most_urgent
            progress = prog_val

        app: str | None = None
        if "app" in fields:
            raw_app = fields["app"]
            if not _APP_RE.match(raw_app):
                return False, self.most_urgent
            app = raw_app

        title: str | None = None
        if "title" in fields:
            title = _decode_b64_text(
                fields["title"],
                max_b64=MAX_TITLE_B64_BYTES,
                max_decoded=MAX_TITLE_BYTES,
                allow_tab_newline=False,
            )
            if title is None:
                return False, self.most_urgent

        msg: str | None = None
        if "msg" in fields:
            msg = _decode_b64_text(
                fields["msg"],
                max_b64=MAX_MSG_B64_BYTES,
                max_decoded=MAX_MSG_BYTES,
                allow_tab_newline=True,
            )
            if msg is None:
                return False, self.most_urgent

        if task_id not in self._records and len(self._records) >= self._max_records:
            # Evict oldest non-root entry when full.
            victim = next((k for k in self._records if k != ""), None)
            if victim is not None:
                del self._records[victim]
            else:
                return False, self.most_urgent

        effective_app = self._resolve_effective_app(task_id, app)
        record = ProgramStatus(
            state=state,
            id=task_id,
            kind=kind,
            progress=progress,
            app=app,
            effective_app=effective_app,
            title=title,
            msg=msg,
        )
        prev = self._records.get(task_id)
        self._records[task_id] = record
        return prev != record, record

    def apply_osc_9_4(
        self, submode: int, value: int | None
    ) -> tuple[bool, ProgramStatus | None]:
        """Map ConEmu ``OSC 9;4;st;prog`` to root status before ``7501`` arrives."""
        if self._native_7501_seen:
            return False, self.most_urgent
        prog = None if value is None else max(0, min(100, value))
        if submode == 0:
            if "" not in self._records:
                return False, self.most_urgent
            del self._records[""]
            return True, self.most_urgent
        if submode in (1, 3):
            state = StatusState.WORKING
            if submode == 3:
                prog = None
        elif submode == 2:
            state = StatusState.ERROR
        elif submode == 4:
            state = StatusState.BLOCKED
        else:
            return False, self.most_urgent
        rec = ProgramStatus(state=state, id="", progress=prog)
        prev = self._records.get("")
        self._records[""] = rec
        return prev != rec, rec

    def on_prompt_start(self) -> bool:
        """Prune ``working``, ``blocked``, and ``idle`` tasks at shell prompt start."""
        stale = [
            k
            for k, r in self._records.items()
            if r.state in (StatusState.WORKING, StatusState.BLOCKED, StatusState.IDLE)
        ]
        for k in stale:
            del self._records[k]
        return bool(stale)

    def on_process_exit(self) -> bool:
        """Prune ``working``, ``blocked``, and ``idle`` tasks on process exit."""
        return self.on_prompt_start()

    def reset(self) -> None:
        """Clear all task records and re-enable ``OSC 9;4`` fallback (``RIS``)."""
        self._records.clear()
        self._native_7501_seen = False


__all__ = [
    "BlockKind",
    "ProgramStatus",
    "ProgramStatusStore",
    "StatusState",
]
