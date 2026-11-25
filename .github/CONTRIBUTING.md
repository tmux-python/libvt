# Contributing

Thanks for looking. Bug reports with a minimal byte-stream reproduction,
and notes on where the documentation misled you, are the most useful
things right now.

How this project writes prose — `README.md`, `CHANGES`, release notes,
commit messages, docstrings, and source comments — is set out separately
in [WRITING.md](WRITING.md). Read that before changing any of it. The
constraints every change is held to, and the map of what is where, are
in [AGENTS.md](../AGENTS.md).

## Getting set up

Install [git] and [uv].

```console
$ git clone https://github.com/tmux-python/libvt.git
```

```console
$ cd libvt
```

```console
$ uv sync --all-groups
```

This project uses Python 3.12+, [uv] for dependency management, [ruff]
for linting and formatting, [ty] for static type checking, and [pytest]
for testing (with [pytest-watcher] for continuous runs).

[git]: https://git-scm.com/
[uv]: https://github.com/astral-sh/uv
[ruff]: https://github.com/astral-sh/ruff
[ty]: https://github.com/astral-sh/ty
[pytest]: https://docs.pytest.org/
[pytest-watcher]: https://github.com/olzhasar/pytest-watcher

## The gates

Format:

```console
$ uv run ruff format .
```

Lint:

```console
$ uv run ruff check . --fix --show-fixes
```

Type-check:

```console
$ uv run ty check
```

Test:

```console
$ uv run pytest
```

Documentation is a gate, not a courtesy. Examples in docstrings,
documentation pages, and `README.md` are executed by `pytest`; the
doctest flags live in `pyproject.toml`, so there is no separate doctest
step and a green `pytest` is the proof. Which blocks qualify, and the
one mistake that silently removes a test, are in
[WRITING.md](WRITING.md#documented-examples-that-run).

Before claiming a test or a gate works, show it failing. A gate that
has never been red is an assumption.

### Clean-room provenance

`libvt` is MIT-licensed and keeps zero runtime dependencies. Derive
protocol state machines, escape parameters, and test vectors strictly
from public standards (ECMA-48, ITU T.416, Unicode UAX #11 / #29, Paul
Flo Williams' VT500 parser description) and permissively licensed
(`MIT`, `ISC`, `Apache-2.0`, `BSD`) specifications and implementations.
Never copy, adapt, or translate implementation code from `LGPL` or `GPL`
projects (`pyte`, `libvte`, `asciinema`).

### Code style

Standard library imports are namespace imports —
`import pathlib`, not `from pathlib import Path` — except
`dataclasses`, which may use `from dataclasses import dataclass, field`
for the decorator syntax. Third-party packages may use `from X import Y`
freely. For `typing`, `import typing as t` and access members via the
namespace (`t.NamedTuple`, `t.TypeAlias`, …). Every file starts with
`from __future__ import annotations`; ruff's isort `required-imports`
enforces it.

Docstring conventions, including the NumPy style all public functions
and methods use, are in [WRITING.md](WRITING.md#docstrings).

## Tests

The core test suite runs in pure Python with no external binaries
required. `libvt`'s own pytest plugin (`src/libvt/pytest_plugin.py`)
exposes `vt_`-prefixed fixtures (`vt_terminal`, `vt_factory`,
`vt_parser`) with lazy imports inside fixture bodies so test collection
stays free of startup overhead.

Write tests as standalone functions, not `class TestFoo:` groupings —
descriptive function names and file organization carry the structure
instead. Prefer real `Terminal` and `Parser` instances over
`monkeypatch` and `MagicMock`; elsewhere, prefer `tmp_path`
(`pathlib.Path`) over `tempfile`.

Run the suite continuously during development:

```console
$ uv run ptw .
```

## Documentation

Build once:

```console
$ just build-docs
```

Serve with live reload while editing (default: http://localhost:8024):

```console
$ just start-docs
```

`just build-docs` is also how a broken cross-reference or MyST role
surfaces — the doctests do not catch it. Nothing under `docs/_build/`
is hand-edited; it is the build's own output and is not tracked. The
prose voice, cross-reference conventions, and doctest rules for pages
under `docs/` are in [WRITING.md](WRITING.md).

## Releasing

Never create tags. Never push tags. The owner handles tagging and tag
pushes, because a tag triggers the publish workflow. See
[Release commits](WRITING.md#release-commits).

`libvt` is pre-1.0 (currently in `0.0.x` pre-alpha): `0.0.x` updates and `0.x`
minor version bumps may include breaking API changes, so downstream projects
should pin `libvt>=0.0.1a0,<0.1` (and `>=0.x,<0.y` from `0.1.0` onward).

1. Add the release's entries to `CHANGES`, following
   [The changelog](WRITING.md#the-changelog).
2. Bump the version in both `pyproject.toml` and
   `src/libvt/__about__.py` — neither is derived from the other.
3. Commit as `Tag v<version>` (see
   [Release commits](WRITING.md#release-commits)).
4. The owner tags (`git tag v<version>`) and pushes the tag. CI then
   builds and publishes to PyPI via OIDC trusted publishing —
   there is no manual upload step.

## Pull requests

One subject per pull request. Unrelated cleanup found along the way
belongs in its own commit, and usually in its own pull request.

Discuss a substantial change via an issue or ADR before making it.

Update the docs when a change touches the public interface.

Commit format is in [WRITING.md](WRITING.md#commits).

## Decorum

- Participants will be tolerant of opposing views.
- Participants must ensure that their language and actions are free of
  personal attacks and disparaging personal remarks.
- When interpreting the words and actions of others, participants
  should always assume good intentions.
- Behaviour which can be reasonably considered harassment will not be
  tolerated.

Based on [Ruby's Community Conduct Guideline](https://www.ruby-lang.org/en/conduct/).

## Security

Please do not open a public issue for a vulnerability. Report it
privately via
[GitHub Security Advisories](https://github.com/tmux-python/libvt/security/advisories/new).
