"""Package smoke and import-hygiene tests for libvt."""

from __future__ import annotations

import subprocess
import sys

import libvt


def test_package_metadata() -> None:
    """Package metadata fields match the published distribution identity."""
    assert libvt.__title__ == "libvt"
    assert libvt.__package_name__ == "libvt"
    assert libvt.__license__ == "MIT"
    assert libvt.__version__.startswith("0.1.")


def test_import_libvt_leaves_optional_subsystems_unloaded() -> None:
    """Importing ``libvt`` in a fresh interpreter does not import ``asyncio``."""
    proc = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "import sys, libvt; "
                "assert 'asyncio' not in sys.modules, list(sys.modules); "
                "assert 'pytest' not in sys.modules, list(sys.modules)"
            ),
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
