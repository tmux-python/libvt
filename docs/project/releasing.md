# Releasing

## Version Policy

`libvt` is pre-1.0 (currently in `0.0.x` pre-alpha). Pre-`0.1` updates and
`0.x` minor version bumps may include breaking API changes; downstream users
should pin `libvt>=0.0.1a0,<0.1` (and `>=0.x,<0.y` once `0.1.0` ships).

## Release Process

Releases are triggered by git tags and published to [PyPI] via OIDC
trusted publishing.

1. Update `CHANGES` with the release notes.
2. Bump version in `pyproject.toml` and `src/libvt/__about__.py`.
3. Commit:

   ```console
   $ git commit -m "Tag v<version>"
   ```

4. Tag:

   ```console
   $ git tag v<version>
   ```

5. Push:

   ```console
   $ git push && git push --tags
   ```

6. CI builds and publishes to [PyPI] via trusted publishing.

[PyPI]: https://pypi.org/project/libvt/
