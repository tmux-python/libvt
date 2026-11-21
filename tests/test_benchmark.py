"""Tests for the in-process and cross-commit benchmark helpers."""

from __future__ import annotations

import pathlib

from scripts import bench_vt, benchmark


def test_bench_vt_summarize_and_run() -> None:
    """In-process benchmark statistics and default suite produce valid metrics."""
    stats = bench_vt.summarize([0.010, 0.020, 0.030, 0.040])
    assert stats["n"] == 4.0
    assert stats["min"] == 0.010
    assert stats["max"] == 0.040
    assert stats["p90"] == 0.040

    results = bench_vt.run_benchmarks(warmup=1, runs=2)
    assert len(results) >= 2
    assert results[0].stats_seconds["n"] == 2.0


def test_benchmark_config_layering_and_sanitization(
    tmp_path: pathlib.Path,
) -> None:
    """TOML layers merge cleanly and local paths are sanitized from commands."""
    primary = tmp_path / "benchmark.toml"
    primary.write_text(
        '[settings]\nruns = 3\n\n[bench.smoke]\ncommand = "{venv}/bin/python -V"\n',
        encoding="utf-8",
    )
    local = tmp_path / "benchmark.local.toml"
    local.write_text("[settings]\nruns = 7\n", encoding="utf-8")

    cfg = benchmark.load_config(
        config_path=primary,
        local_path=local,
        cli_overrides={"settings": {"warmup": 4}},
    )
    assert cfg.settings.runs == 7
    assert cfg.settings.warmup == 4
    assert "smoke" in cfg.bench

    sanitized = benchmark.sanitize_command_string(
        "/tmp/work/repo/.venv/bin/python -V",
        {"repo": "/tmp/work/repo", "venv": "/tmp/work/repo/.venv"},
    )
    assert sanitized == "{venv}/bin/python -V"
