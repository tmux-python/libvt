"""Verify that every dataclass in ``src/libvt`` documents its ``Attributes``."""

from __future__ import annotations

import importlib
import inspect
import re
from dataclasses import fields, is_dataclass

_MODULES = (
    "libvt.style",
    "libvt.modes",
    "libvt._grid",
    "libvt.parser",
    "libvt.screen",
    "libvt.terminal",
)

_ATTR_HEADER_RE = re.compile(r"^Attributes\s*\n-+\s*$", re.MULTILINE)
_FIELD_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")


def _documented_attributes(doc: str) -> list[str]:
    match = _ATTR_HEADER_RE.search(doc)
    if match is None:
        return []
    tail = doc[match.end() :].splitlines()
    names: list[str] = []
    for line in tail:
        if line and not line[0].isspace():
            fm = _FIELD_LINE_RE.match(line)
            if fm is not None:
                names.append(fm.group(1))
    return names


def test_every_dataclass_documents_all_fields_in_order() -> None:
    """Every ``@dataclass`` in ``libvt`` lists all fields in its NumPy docstring."""
    mismatches: dict[str, tuple[list[str], list[str]]] = {}
    for mod_name in _MODULES:
        mod = importlib.import_module(mod_name)
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ != mod_name or not is_dataclass(obj):
                continue
            expected = [f.name for f in fields(obj)]
            doc = inspect.getdoc(obj) or ""
            documented = _documented_attributes(doc)
            if documented != expected:
                mismatches[f"{mod_name}.{name}"] = (expected, documented)
    assert not mismatches, f"Dataclass Attributes mismatches: {mismatches}"
