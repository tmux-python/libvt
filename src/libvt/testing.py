"""Column-exact visual boxed screen formatting and test assertions for libvt."""

from __future__ import annotations

import collections.abc
import textwrap

from libvt._grid import raw_width_bits
from libvt._ucd import (
    WidthRole,
    char_width,
    display_width,
    sanitize_terminal_text,
)
from libvt.codec import format_explain
from libvt.exc import ScreenAssertionError
from libvt.terminal import Snapshot, Terminal


def _format_row_cells(snap: Snapshot, row_idx: int, width: int) -> str:
    row = snap._rows[row_idx]
    parts: list[str] = []
    cols_used = 0
    c = 0
    limit = min(width, snap.cols)
    while c < limit and cols_used < width:
        word = row.attr_at(c)
        rb = raw_width_bits(word)
        if rb == int(WidthRole.SPACER_TAIL):
            c += 1
            continue
        if rb == int(WidthRole.SPACER_HEAD):
            parts.append(" ")
            cols_used += 1
            c += 1
            continue
        cell_w = 2 if rb == int(WidthRole.WIDE) else 1
        if cols_used + cell_w > width:
            parts.append(" ")
            cols_used += 1
            break
        parts.append(sanitize_terminal_text(row.char_at(c)))
        cols_used += cell_w
        c += 1
    if cols_used < width:
        parts.append(" " * (width - cols_used))
    return "".join(parts)


def _pad_expected_line(line: str, width: int) -> str:
    sanitized = sanitize_terminal_text(line)
    out: list[str] = []
    cols_used = 0
    for ch in sanitized:
        cw = char_width(ch)
        if cols_used + cw > width:
            break
        out.append(ch)
        cols_used += cw
    if cols_used < width:
        out.append(" " * (width - cols_used))
    return "".join(out)


def format_screen_box(
    target: Terminal | Snapshot,
    *,
    rows: int | None = None,
    cols: int | None = None,
) -> str:
    """Render *target* inside a column-aligned Unicode ``┌──┐`` / ``│..│`` frame.

    Soft-wrapped rows display a ``W`` marker immediately right of the right
    border, and C0/C1/BiDi override codepoints are escaped safely.
    """
    snap = target.snapshot() if isinstance(target, Terminal) else target
    h = snap.rows if rows is None else min(rows, snap.rows)
    w = snap.cols if cols is None else min(cols, snap.cols)
    border_h = "─" * w
    out = [f"┌{border_h}┐"]
    for r in range(h):
        content = _format_row_cells(snap, r, w)
        wrap_mark = "W" if snap._rows[r].wrapped else ""
        out.append(f"│{content}│{wrap_mark}")
    out.append(f"└{border_h}┘")
    return "\n".join(out)


def format_flight_recorder(target: Terminal) -> str:
    """Render compact flight-recorder state and recent disassembled VT bytes."""
    modes_str = ", ".join(m.name for m in sorted(target.modes.enabled_modes(), key=int))
    lines = [
        "Flight Recorder:",
        (
            f"  cursor=({target.cursor.row}, {target.cursor.col}) "
            f"pending_wrap={target.cursor.pending_wrap} "
            f"alt={target.screens.active_is_alt} "
            f"parser={target.parser_state.name}"
        ),
        f"  modes=[{modes_str}]",
    ]
    if target.program_status is not None:
        ps = target.program_status
        lines.append(
            f"  program_status={ps.state.value} id={ps.id!r} progress={ps.progress}",
        )
    rec = target.recorder
    if rec is not None:
        tail = rec.tail(256)
        lines.append(f"  recent_bytes ({len(tail)}/{rec.total_bytes}B): {tail!r}")
        explained = format_explain(tail)
        if explained:
            lines.extend(f"    {ln}" for ln in explained.splitlines()[-12:])
    return "\n".join(lines)


def _raise_mismatch(
    target: Terminal | Snapshot,
    snap: Snapshot,
    expected_box: str,
    actual_box: str,
    cursor: tuple[int, int] | None,
) -> None:
    details = [
        "Screen state mismatch:",
        f"cursor actual=({snap.cursor.row}, {snap.cursor.col})"
        + (f" expected={cursor}" if cursor is not None else ""),
        "Expected:",
        expected_box,
        "Actual:",
        actual_box,
    ]
    if isinstance(target, Terminal):
        details.append(format_flight_recorder(target))
    raise ScreenAssertionError("\n".join(details))


def assert_screen(
    target: Terminal | Snapshot,
    expected: str | collections.abc.Sequence[str],
    *,
    cursor: tuple[int, int] | None = None,
) -> None:
    """Assert that *target*'s literal visible text (and *cursor*) matches *expected*.

    Unlike :func:`assert_screen_box`, ``assert_screen`` never strips ``┌──┐``
    border glyphs, so applications drawing literal box-drawing borders match
    verbatim.

    Parameters
    ----------
    target : Terminal | Snapshot
        Live terminal or immutable snapshot under test.
    expected : str | Sequence[str]
        Expected visible text (dedented when passed as a multi-line string)
        or explicit sequence of right-trimmed row strings.
    cursor : tuple[int, int] | None, optional
        Expected zero-based ``(row, col)`` cursor coordinates, if checked.

    Raises
    ------
    ScreenAssertionError
        When visible lines or cursor coordinates differ from *expected*.
    """
    snap = target.snapshot() if isinstance(target, Terminal) else target
    if isinstance(expected, str):
        cleaned = textwrap.dedent(expected).removeprefix("\n").removesuffix("\n")
        exp_lines = cleaned.split("\n") if cleaned else []
    else:
        exp_lines = list(expected)

    actual_text = snap.text
    act_lines = actual_text.split("\n") if actual_text else []

    cursor_ok = cursor is None or (snap.cursor.row, snap.cursor.col) == cursor
    if act_lines == exp_lines and cursor_ok:
        return

    preview_rows = min(snap.rows, max(len(exp_lines), len(act_lines), 1))
    max_exp_w = max((display_width(x) for x in exp_lines), default=0)
    max_act_w = max((display_width(x) for x in act_lines), default=0)
    preview_cols = min(snap.cols, max(max_exp_w, max_act_w, 12))

    actual_box = format_screen_box(snap, rows=preview_rows, cols=preview_cols)
    exp_border = "─" * preview_cols
    exp_box_lines = [f"┌{exp_border}┐"]
    for idx in range(preview_rows):
        line = exp_lines[idx] if idx < len(exp_lines) else ""
        exp_box_lines.append(f"│{_pad_expected_line(line, preview_cols)}│")
    exp_box_lines.append(f"└{exp_border}┘")
    _raise_mismatch(target, snap, "\n".join(exp_box_lines), actual_box, cursor)


def assert_screen_box(
    target: Terminal | Snapshot,
    expected: str,
    *,
    cursor: tuple[int, int] | None = None,
) -> None:
    """Assert that *target* matches a ``┌──┐`` / ``│..│`` boxed diagram.

    Validates exact display-column width across every row, preserves interior
    and trailing spaces as well as blank rows, and checks soft-wrap flags via
    trailing ``│W`` markers outside the right pipe.

    Parameters
    ----------
    target : Terminal | Snapshot
        Live terminal or immutable snapshot under test.
    expected : str
        Boxed diagram matching :func:`format_screen_box` layout.
    cursor : tuple[int, int] | None, optional
        Expected zero-based ``(row, col)`` cursor coordinates, if checked.

    Raises
    ------
    ValueError
        When *expected* does not form a rectangular ``┌──┐`` / ``└──┘`` frame.
    ScreenAssertionError
        When screen dimensions, cell contents, soft-wrap flags, or cursor
        coordinates differ from *expected*.
    """
    cleaned = textwrap.dedent(expected).removeprefix("\n").removesuffix("\n")
    raw_lines = cleaned.split("\n") if cleaned else []
    if len(raw_lines) < 3:
        msg = "expected box must contain top border, >=1 row, and bottom border"
        raise ValueError(msg)

    top = raw_lines[0]
    bottom = raw_lines[-1]
    if not (top.startswith("┌") and top.endswith("┐")):
        msg = f"expected box top border must match '┌─...─┐', got {top!r}"
        raise ValueError(msg)
    top_bars = top[1:-1]
    if not top_bars or any(ch != "─" for ch in top_bars):
        msg = f"expected box top border must contain only '─', got {top!r}"
        raise ValueError(msg)
    box_cols = len(top_bars)
    expected_bottom = f"└{'─' * box_cols}┘"
    if bottom != expected_bottom:
        msg = f"expected box bottom border {expected_bottom!r}, got {bottom!r}"
        raise ValueError(msg)

    norm_lines = [top]
    for r_idx, raw_row in enumerate(raw_lines[1:-1]):
        if not raw_row.startswith("│"):
            msg = f"box row {r_idx} missing leading '│': {raw_row!r}"
            raise ValueError(msg)
        if raw_row.endswith("│W"):
            body = raw_row[1:-2]
            wrap_suffix = "W"
        elif raw_row.endswith("│"):
            body = raw_row[1:-1]
            wrap_suffix = ""
        else:
            msg = f"box row {r_idx} must end with '│' or '│W': {raw_row!r}"
            raise ValueError(msg)
        row_w = display_width(body)
        if row_w != box_cols:
            msg = (
                f"box row {r_idx} display width {row_w} != border width "
                f"{box_cols}: {raw_row!r}"
            )
            raise ValueError(msg)
        norm_lines.append(f"│{sanitize_terminal_text(body)}│{wrap_suffix}")
    norm_lines.append(bottom)
    expected_box = "\n".join(norm_lines)

    snap = target.snapshot() if isinstance(target, Terminal) else target
    actual_box = format_screen_box(snap)
    cursor_ok = cursor is None or (snap.cursor.row, snap.cursor.col) == cursor
    if actual_box == expected_box and cursor_ok:
        return
    _raise_mismatch(target, snap, expected_box, actual_box, cursor)


__all__ = [
    "assert_screen",
    "assert_screen_box",
    "format_flight_recorder",
    "format_screen_box",
]
