"""Declarative VT sequence explainer and ``python -m libvt`` CLI tests."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from libvt.__main__ import main
from libvt.codec import explain, format_explain


def test_codec_explain_and_format_explain() -> None:
    """``explain`` and ``format_explain`` annotate SGR, CSI, OSC, DCS, and UTF-8."""
    sample = (
        b"\x1b[?2026h\x1b[1;38:2::10:20:30mHi\r\n"
        b"\x1b]7501;state=working\x1b\\\x1bP>|libvt\x1b\\\x1b[?2026l"
    )
    items = explain(sample)
    kinds = {it.kind for it in items}
    mnemonics = {it.mnemonic for it in items}
    assert {"CSI", "PRINT", "C0", "OSC", "DCS"} <= kinds
    assert "SGR" in mnemonics
    assert "DECSET" in mnemonics

    rendered = format_explain(sample)
    assert "SGR" in rendered
    assert "7501" in rendered


def test_cli_explain_render_and_strip(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``python -m libvt`` subcommands process files and stdin byte streams."""
    sample_file = tmp_path / "sample.vt"
    sample_file.write_bytes(b"\x1b[1;32mhello\x1b[0m\r\nworld")

    assert main(["strip", str(sample_file)]) == 0
    assert capsys.readouterr().out == "hello\nworld\n"

    assert main(["render", "--rows", "2", "--cols", "8", str(sample_file)]) == 0
    assert capsys.readouterr().out == "hello\nworld\n"

    assert main(["explain", str(sample_file)]) == 0
    explained = capsys.readouterr().out
    assert "SGR" in explained

    proc = subprocess.run(
        (sys.executable, "-m", "libvt", "strip"),
        input=b"\x1b[1;34mpiped\x1b[0m\n",
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == b"piped\n"
