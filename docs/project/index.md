(project)=

# Project

Project guides, compatibility information, and API governance.

::::{grid} 1 1 3 3
:gutter: 2 2 3 3

:::{grid-item-card} Contributing
:link: contributing
:link-type: doc
Development setup, running tests, and submitting pull requests.
:::

:::{grid-item-card} Code Style
:link: code-style
:link-type: doc
[Ruff], [ty], [NumPy] docstrings, and import conventions.
:::

:::{grid-item-card} Releasing
:link: releasing
:link-type: doc
Release checklist and version policy.
:::

:::{grid-item-card} Benchmarking
:link: benchmark
:link-type: doc
In-process VT microbenchmarks and cross-commit timing harness.
:::

::::

## API Governance

::::{grid} 1 2 3 3
:gutter: 2 2 3 3

:::{grid-item-card} Public API
:link: public-api
:link-type: doc
Public boundaries, stability policy, and deprecation process.
:::

:::{grid-item-card} Compatibility
:link: compatibility
:link-type: doc
Python runtime and platform support.
:::

:::{grid-item-card} Deprecations
:link: deprecations
:link-type: doc
Active deprecations and migration guidance.
:::

::::

```{toctree}
:hidden:

contributing
code-style
releasing
benchmark
public-api
compatibility
deprecations
```

[Ruff]: https://docs.astral.sh/ruff/
[ty]: https://docs.astral.sh/ty/
[NumPy]: https://numpydoc.readthedocs.io/en/latest/format.html
