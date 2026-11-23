"""Package smoke, import-hygiene, top-level helper, and docstring tests for libvt."""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
import re
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
    """Importing ``libvt`` in a fresh interpreter avoids heavy stdlib/plugin modules."""
    proc = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "import sys, libvt; "
                "assert 'asyncio' not in sys.modules, list(sys.modules); "
                "assert 'pty' not in sys.modules, list(sys.modules); "
                "assert 'termios' not in sys.modules, list(sys.modules); "
                "assert 'pytest' not in sys.modules, list(sys.modules)"
            ),
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_top_level_render_strip_and_unicode_helpers() -> None:
    """Top-level progressive-disclosure helpers render, strip, and measure text."""
    rendered = libvt.render(b"\x1b[1;32mok\x1b[0m\r\ndone", rows=4, cols=10)
    assert rendered == "ok\ndone"

    term = libvt.Terminal(rows=4, cols=10)
    term.feed(b"\x1b[1;32mok\x1b[0m\r\ndone")
    assert term[0, 0].style.fg == libvt.AnsiColor.GREEN

    assert libvt.strip(b"\x1b[31mred\x1b[0m green") == "red green"
    assert libvt.UNICODE_VERSION >= "15.0.0"
    assert libvt.display_width("日本") == 4
    assert libvt.display_width("🧑‍🌾") == 2
    assert libvt.sanitize_terminal_text("safe\u202eevil\x07") == "safe\\u202eevil\\x07"


def test_public_dataclass_attributes_documented() -> None:
    """Every public dataclass documents all public fields under ``Attributes``."""
    missing: list[str] = []
    for modinfo in pkgutil.iter_modules(libvt.__path__, "libvt."):
        if modinfo.name.startswith("libvt._"):
            continue
        mod = importlib.import_module(modinfo.name)
        for cls_name, cls_obj in inspect.getmembers(mod, inspect.isclass):
            if cls_obj.__module__ != modinfo.name or cls_name.startswith("_"):
                continue
            if not dataclasses.is_dataclass(cls_obj):
                continue
            doc = inspect.getdoc(cls_obj) or ""
            for fld in dataclasses.fields(cls_obj):
                if fld.name.startswith("_"):
                    continue
                if not re.search(rf"^\s*{re.escape(fld.name)}\s*:", doc, re.MULTILINE):
                    missing.append(f"{modinfo.name}.{cls_name}.{fld.name}")
    assert missing == []
