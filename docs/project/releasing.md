# Releasing

## Version Policy

`libvt` is pre-1.0. Minor version bumps may include breaking API
changes. Downstream users should pin to `>=0.x,<0.y`.

## Release Process

Releases are triggered by git tags and published to [PyPI] via OIDC
trusted publishing.

1. Update `CHANGES` with the release notes.
2. Bump version in `pyproject.toml` and `src/libvt/__about__.py`.
3. Commit:

   ```console
   $ git commit -m "libvt <version>"
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
