"""Root pytest configuration and doctest namespace setup for libvt."""

from __future__ import annotations

import typing as t

import pytest

import libvt


@pytest.fixture(autouse=True)
def _seed_doctest_namespace(doctest_namespace: dict[str, t.Any]) -> None:
    """Populate doctest globals with ``libvt``."""
    doctest_namespace["libvt"] = libvt
