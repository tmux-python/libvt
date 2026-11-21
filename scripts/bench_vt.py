"""In-process throughput and latency microbenchmarks for libvt."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import statistics
import time
import typing as t
from dataclasses import asdict, dataclass

import libvt

STAT_LABELS: tuple[str, ...] = (
    "n",
    "min",
    "avg",
    "median",
    "p90",
    "p95",
    "p99",
    "max",
)


def percentile(sorted_vals: t.Sequence[float], pct: float) -> float:
    """Compute the nearest-rank percentile of a pre-sorted sequence.

    Parameters
    ----------
    sorted_vals : Sequence[float]
        Ascending numeric sample sequence.
    pct : float
        Requested percentile in ``(0, 100]``.

    Returns
    -------
    float
        Nearest-rank sample value, or ``nan`` when *sorted_vals* is empty.

    Examples
    --------
    >>> percentile([1.0, 2.0, 3.0, 4.0], 50)
    2.0
    >>> percentile([1.0, 2.0, 3.0, 4.0], 100)
    4.0
    """
    if not sorted_vals:
        return float("nan")
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_vals)))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


def summarize(samples: t.Sequence[float]) -> dict[str, float]:
    """Summarize *samples* with min, mean, median, p90, p95, p99, and max.

    Parameters
    ----------
    samples : Sequence[float]
        Raw timing samples in seconds.

    Returns
    -------
    dict[str, float]
        Summary dictionary keyed by :data:`STAT_LABELS`.
    """
    if not samples:
        return {label: 0.0 if label == "n" else float("nan") for label in STAT_LABELS}
    ordered = sorted(samples)
    return {
        "n": float(len(ordered)),
        "min": ordered[0],
        "avg": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "p90": percentile(ordered, 90),
        "p95": percentile(ordered, 95),
        "p99": percentile(ordered, 99),
        "max": ordered[-1],
    }


@dataclass(frozen=True, slots=True)
class WorkloadResult:
    """Timing and throughput summary for one in-process benchmark workload.

    Attributes
    ----------
    name : str
        Stable workload identifier.
    bytes_per_op : int
        Input byte count processed per timed iteration.
    stats_seconds : dict[str, float]
        Sample distribution in seconds keyed by :data:`STAT_LABELS`.
    mib_per_sec_p50 : float
        Median throughput in MiB/s when ``bytes_per_op > 0``, else ``0.0``.
    ops_per_sec_p50 : float
        Median operation rate in operations per second.
    """

    name: str
    bytes_per_op: int
    stats_seconds: dict[str, float]
    mib_per_sec_p50: float
    ops_per_sec_p50: float


def _build_synthetic_ansi_frame() -> bytes:
    """Build a deterministic 80x24 ANSI frame payload."""
    chunks: list[bytes] = [b"\x1b[?2026h\x1b[H"]
    for row in range(1, 25):
        line = f"\x1b[{row};1H\x1b[38;2;40;180;160;1mrow-{row:02d} \x1b[0m" + (
            "abcdef0123456789" * 4
        )
        chunks.append(line.encode("ascii"))
    chunks.append(b"\x1b[?2026l")
    return b"".join(chunks)


def _measure(
    name: str,
    fn: t.Callable[[], None],
    *,
    bytes_per_op: int,
    warmup: int,
    runs: int,
) -> WorkloadResult:
    """Run *fn* with *warmup* + *runs* iterations and summarize timings."""
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    stats = summarize(samples)
    median_s = stats["median"]
    ops_s = (1.0 / median_s) if median_s > 0 else 0.0
    mib_s = (
        (bytes_per_op / (1024.0 * 1024.0)) / median_s
        if bytes_per_op > 0 and median_s > 0
        else 0.0
    )
    return WorkloadResult(
        name=name,
        bytes_per_op=bytes_per_op,
        stats_seconds=stats,
        mib_per_sec_p50=mib_s,
        ops_per_sec_p50=ops_s,
    )


def run_benchmarks(
    *,
    warmup: int = 2,
    runs: int = 10,
) -> list[WorkloadResult]:
    """Execute the default in-process benchmark suite.

    Parameters
    ----------
    warmup : int, optional
        Discarded warmup iterations per workload, by default ``2``.
    runs : int, optional
        Measured iterations per workload, by default ``10``.

    Returns
    -------
    list[WorkloadResult]
        Measured results in deterministic workload order.
    """
    frame = _build_synthetic_ansi_frame()
    batch = frame * 16
    results: list[WorkloadResult] = [
        _measure(
            "reload-metadata",
            lambda: importlib.reload(libvt.__about__),  # type: ignore[attr-defined]
            bytes_per_op=0,
            warmup=warmup,
            runs=runs,
        ),
        _measure(
            "utf8-decode-baseline",
            lambda: _consume_str(batch.decode("utf-8", errors="replace")),
            bytes_per_op=len(batch),
            warmup=warmup,
            runs=runs,
        ),
    ]
    terminal_cls = getattr(libvt, "Terminal", None)
    if terminal_cls is not None:
        term = terminal_cls(rows=24, cols=80)
        results.append(
            _measure(
                "terminal-feed-80x24x16",
                lambda: term.feed(batch),
                bytes_per_op=len(batch),
                warmup=warmup,
                runs=runs,
            ),
        )
    return results


def _consume_str(value: str) -> None:
    """Anchor decoded string length so the call cannot be dead-code eliminated."""
    if not value:
        msg = "expected non-empty decoded frame"
        raise AssertionError(msg)


def _format_text(results: t.Sequence[WorkloadResult]) -> str:
    """Format benchmark results as an aligned plain-text table."""
    header = (
        f"{'workload':<26} {'p50 (ms)':>10} {'p95 (ms)':>10} "
        f"{'ops/s':>12} {'MiB/s':>10}"
    )
    lines = [header, "-" * len(header)]
    for item in results:
        p50_ms = item.stats_seconds["median"] * 1000.0
        p95_ms = item.stats_seconds["p95"] * 1000.0
        lines.append(
            f"{item.name:<26} {p50_ms:>10.3f} {p95_ms:>10.3f} "
            f"{item.ops_per_sec_p50:>12.1f} {item.mib_per_sec_p50:>10.2f}",
        )
    return "\n".join(lines)


def main(argv: t.Sequence[str] | None = None) -> int:
    """CLI entry point for ``scripts/bench_vt.py``."""
    parser = argparse.ArgumentParser(
        description="Run in-process libvt throughput microbenchmarks.",
    )
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
    )
    args = parser.parse_args(argv)
    results = run_benchmarks(warmup=args.warmup, runs=args.runs)
    if args.output_format == "json":
        payload = {
            "package": libvt.__package_name__,
            "version": libvt.__version__,
            "workloads": [asdict(item) for item in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(_format_text(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
