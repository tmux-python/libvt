"""Command-line entry point for ``python -m libvt`` (explain, render, strip)."""

from __future__ import annotations

import argparse
import pathlib
import sys

from libvt.codec import format_explain
from libvt.terminal import render, strip


def _read_input(path: str | None) -> bytes:
    if path is None or path == "-":
        return sys.stdin.buffer.read()
    return pathlib.Path(path).read_bytes()


def main(argv: list[str] | None = None) -> int:
    """Run ``python -m libvt`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m libvt",
        description="Inspect, render, or strip VT byte streams.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_explain = sub.add_parser("explain", help="Disassemble VT escape sequences")
    p_explain.add_argument("file", nargs="?", default="-", help="Input file or '-'")

    p_render = sub.add_parser("render", help="Render VT stream to plain screen text")
    p_render.add_argument("file", nargs="?", default="-", help="Input file or '-'")
    p_render.add_argument("--rows", type=int, default=24, help="Screen rows")
    p_render.add_argument("--cols", type=int, default=80, help="Screen columns")

    p_strip = sub.add_parser("strip", help="Strip VT control sequences from input")
    p_strip.add_argument("file", nargs="?", default="-", help="Input file or '-'")

    args = parser.parse_args(argv)
    raw = _read_input(args.file)

    if args.command == "explain":
        out = format_explain(raw)
    elif args.command == "render":
        out = render(raw, rows=args.rows, cols=args.cols)
    else:
        out = strip(raw)

    if out:
        sys.stdout.write(out + ("\n" if not out.endswith("\n") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
