"""Root pytest configuration and doctest namespace setup for libvt."""

from __future__ import annotations

import typing as t

import pytest

import libvt
from libvt.testing import assert_screen, assert_screen_box


@pytest.fixture(autouse=True)
def _seed_doctest_namespace(doctest_namespace: dict[str, t.Any]) -> None:
    """Populate doctest globals with common ``libvt`` entry points."""
    doctest_namespace["libvt"] = libvt
    doctest_namespace["Terminal"] = libvt.Terminal
    doctest_namespace["AnsiColor"] = libvt.AnsiColor
    doctest_namespace["assert_screen"] = assert_screen
    doctest_namespace["assert_screen_box"] = assert_screen_box
