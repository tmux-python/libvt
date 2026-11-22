"""Visual boxed screen formatting and test assertions for libvt."""

from __future__ import annotations

import collections.abc
import textwrap

from libvt.exc import ScreenAssertionError
from libvt.terminal import Snapshot, Terminal


def format_screen_box(
    target: Terminal | Snapshot,
    *,
    rows: int | None = None,
    cols: int | None = None,
) -> str:
    """Render *target* inside a Unicode ``┌──┐`` / ``│..│`` / ``└──┘`` frame.

    Soft-wrapped rows display a ``W`` marker immediately right of the right
    border so soft-wrap vs hard-newline regressions are visible at a glance.
    """
    snap = target.snapshot() if isinstance(target, Terminal) else target
    h = snap.rows if rows is None else min(rows, snap.rows)
    w = snap.cols if cols is None else min(cols, snap.cols)
    border_h = "─" * w
    out = [f"┌{border_h}┐"]
    for r in range(h):
        row = snap._rows[r]
        content = row.rstrip_text()[:w].ljust(w)
        wrap_mark = "W" if row.wrapped else ""
        out.append(f"│{content}│{wrap_mark}")
    out.append(f"└{border_h}┘")
    return "\n".join(out)


def assert_screen(
    target: Terminal | Snapshot,
    expected: str | collections.abc.Sequence[str],
    *,
    cursor: tuple[int, int] | None = None,
) -> None:
    """Assert that *target*'s visible lines (and optional *cursor*) match *expected*.

    Parameters
    ----------
    target : Terminal | Snapshot
        Live terminal or immutable snapshot under test.
    expected : str | Sequence[str]
        Expected visible text (dedented when passed as a multi-line string)
        or explicit sequence of row strings.
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

    preview_rows = max(len(exp_lines), len(act_lines), 1)
    preview_cols = min(
        snap.cols,
        max(
            [*(len(x) for x in exp_lines), *(len(x) for x in act_lines), 12],
        ),
    )
    actual_box = format_screen_box(snap, rows=preview_rows, cols=preview_cols)
    exp_border = "─" * preview_cols
    exp_box_lines = [f"┌{exp_border}┐"]
    for idx in range(preview_rows):
        line = exp_lines[idx] if idx < len(exp_lines) else ""
        exp_box_lines.append(f"│{line[:preview_cols].ljust(preview_cols)}│")
    exp_box_lines.append(f"└{exp_border}┘")
    expected_box = "\n".join(exp_box_lines)

    details = [
        "Screen state mismatch:",
        f"cursor actual=({snap.cursor.row}, {snap.cursor.col})"
        + (f" expected={cursor}" if cursor is not None else ""),
        "Expected:",
        expected_box,
        "Actual:",
        actual_box,
    ]
    raise ScreenAssertionError("\n".join(details))


__all__ = [
    "assert_screen",
    "format_screen_box",
]
