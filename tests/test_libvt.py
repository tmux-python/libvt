"""Package smoke, import-hygiene, top-level helper, and docstring tests for libvt."""

from __future__ import annotations

import dataclasses
import importlib
import importlib.metadata
import inspect
import pathlib
import pkgutil
import re
import subprocess
import sys
import tomllib

import libvt


def test_package_metadata() -> None:
    """Package metadata fields match the published distribution identity."""
    assert libvt.__title__ == "libvt"
    assert libvt.__package_name__ == "libvt"
    assert libvt.__license__ == "MIT"
    assert libvt.__version__.startswith("0.0.")


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
    """Top-level helpers render, strip, and measure terminal text."""
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


def test_cross_platform_classifiers_and_ci_workflows() -> None:
    """Dist metadata, ``pyproject.toml``, and CI declare Linux, macOS, and Windows."""
    expected_os = {
        "Operating System :: POSIX",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
    }
    dist_classifiers = set(
        importlib.metadata.metadata("libvt").get_all("Classifier") or [],
    )
    assert expected_os <= dist_classifiers

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    pyproject_path = repo_root / "pyproject.toml"
    if pyproject_path.is_file():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        assert expected_os <= set(data["project"]["classifiers"])

    if (repo_root / ".github").is_dir():
        tests_wf = repo_root / ".github" / "workflows" / "tests.yml"
        docs_wf = repo_root / ".github" / "workflows" / "docs.yml"
        assert tests_wf.is_file()
        assert docs_wf.is_file()
        tests_txt = tests_wf.read_text(encoding="utf-8")
        docs_txt = docs_wf.read_text(encoding="utf-8")
        assert "macos-latest" in tests_txt
        assert "windows-latest" in tests_txt
        assert '--paths "/*"' in docs_txt
