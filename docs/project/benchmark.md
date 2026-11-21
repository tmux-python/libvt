(benchmark)=

# Benchmarking

`libvt` ships two developer-only benchmark runners under `scripts/`:

- `scripts/bench_vt.py`: in-process throughput and percentile suite for
  parser, screen, and import workloads (`just bench`).
- `scripts/benchmark.py`: cross-commit runner (`hyperfine` with a
  pure-Python fallback) driven by `scripts/benchmark.toml`.

Neither script is packaged in the published wheel.

## In-process microbenchmarks

Run the default suite:

```console
$ just bench
```

Emit machine-readable JSON:

```console
$ uv run python scripts/bench_vt.py --warmup 2 --runs 10 --format json
```

## Cross-commit harness

Compare `HEAD` against `master`:

```console
$ uv run python scripts/benchmark.py run --head-vs-trunk
```

Inspect resolved configuration and commit selectors without running
benchmarks:

```console
$ uv run python scripts/benchmark.py list-commands
$ uv run python scripts/benchmark.py list-commits --lookback 5
```

Override sample counts or add machine-local commands by copying
`scripts/benchmark.local.toml.example` to `scripts/benchmark.local.toml`
(gitignored).
