(quickstart)=

# Quickstart

## Requirements

- Python >= 3.12 (CPython and PyPy)
- Zero runtime dependencies

## Installation

```console
$ uv add libvt
```

```console
$ pip install libvt
```

## Importing `libvt`

Importing `libvt` performs no I/O and keeps `asyncio` and `pytest`
unloaded until explicitly imported:

```python
>>> import libvt
>>> libvt.__package_name__
'libvt'
>>> libvt.__license__
'MIT'
```
