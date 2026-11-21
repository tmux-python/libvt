# Public API

## What Is Public

Every module documented under [API Reference](../api/index.md) is public
API.

## What Is Internal

Modules prefixed with `_` (such as `libvt._grid` or `libvt._width`) are
not public. They may change or be removed without notice between any
release.

## Pre-1.0 Stability Policy

`libvt` is pre-1.0:

- **Minor versions** (`0.x` -> `0.y`) may include breaking API changes.
- **Patch versions** (`0.x.y` -> `0.x.z`) contain bug fixes only.
- **Pin your dependency**: use `libvt>=0.1,<0.2` or `libvt~=0.1.0`.

Breaking changes are recorded in [changelog](../history.md) and
[migration](../migration.md).

## Deprecation Process

Before removing or changing public API:

1. Emit a `DeprecationWarning` for at least one minor release.
2. Document the replacement in [deprecations](deprecations.md) and
   [migration](../migration.md).
3. Remove the deprecated symbol in a subsequent minor release.
