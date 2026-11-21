"""Pure-Python virtual terminal emulator and streaming VT parser."""

from __future__ import annotations

import logging

from libvt.__about__ import (
    __author__,
    __copyright__,
    __description__,
    __docs__,
    __email__,
    __github__,
    __license__,
    __package_name__,
    __pypi__,
    __title__,
    __tracker__,
    __version__,
)

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "__author__",
    "__copyright__",
    "__description__",
    "__docs__",
    "__email__",
    "__github__",
    "__license__",
    "__package_name__",
    "__pypi__",
    "__title__",
    "__tracker__",
    "__version__",
]
