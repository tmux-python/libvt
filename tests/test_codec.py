"""Tests for ``libvt.codec`` sequence disassembly and ``python -m libvt`` CLI."""

from __future__ import annotations

import io
import pathlib
import sys

import pytest

from libvt.__main__ import main
from libvt.codec import explain, format_explain


def test_explain_disassembles_csi_subparams_osc_dcs_and_hpr_vpr() -> None:
    """``explain`` and ``format_explain`` classify SGR, CSI, OSC, and DCS spans."""
    sample = (
        b"\x1b[?2026h\x1b[1;38:2::10:20:30mHi\x1b[3a\x1b[2e\r\n"
        b"\x1b]7501;state=working\x1b\\\x1bP>|libvt\x1b\\\x1b[?2026l"
    )
    items = explain(sample)
    kinds = {it.kind for it in items}
    mnemonics = [it.mnemonic for it in items]
    assert {"CSI", "PRINT", "C0", "OSC", "DCS"} <= kinds
    assert "SGR" in mnemonics
    assert "DECSET" in mnemonics
    assert "HPR" in mnemonics
    assert "VPR" in mnemonics

    rendered = format_explain(sample)
    assert "SGR" in rendered
    assert "7501" in rendered


def test_cli_subcommands_file_and_stdin(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python -m libvt`` subcommands process file paths and ``stdin`` streams."""
    sample_file = tmp_path / "sample.vt"
    sample_file.write_bytes(b"\x1b[1;32mhello\x1b[0m\r\nworld")

    assert main(["strip", str(sample_file)]) == 0
    assert capsys.readouterr().out == "hello\nworld\n"

    assert main(["render", "--rows", "2", "--cols", "8", str(sample_file)]) == 0
    assert capsys.readouterr().out == "hello\nworld\n"

    assert main(["explain", str(sample_file)]) == 0
    assert "SGR" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b"\x1b[1;34mpiped\x1b[0m\n")),
    )
    assert main(["strip"]) == 0
    assert capsys.readouterr().out == "piped\n"
