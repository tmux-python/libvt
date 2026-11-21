# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "click>=8.1",
#     "typer>=0.12",
#     "rich>=13.9",
#     "pydantic>=2.12",
# ]
# ///
"""Cross-commit benchmark harness for libvt.

Walks hyperfine (or a pure-Python timing fallback) across HEAD, trunk, a
commit range, a lookback window, git tags, or explicit refs. Default
benchmark definitions live in ``scripts/benchmark.toml`` and may be
overridden locally via ``scripts/benchmark.local.toml`` (gitignored).
"""

from __future__ import annotations

import atexit
import contextlib
import csv
import io
import json
import math
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import typing as t
from dataclasses import asdict, dataclass

import click.exceptions
import pydantic
import rich.console
import rich.table
import typer

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "scripts" / "benchmark.toml"
LOCAL_CONFIG = REPO_ROOT / "scripts" / "benchmark.local.toml"

Status = t.Literal[
    "ok",
    "checkout_fail",
    "sync_fail",
    "command_missing",
    "bench_fail",
]
OutputFormat = t.Literal["rich", "json", "ndjson", "md", "csv"]
AnalysisOutputFormat = t.Literal["rich", "json", "ndjson"]
SCHEMA_VERSION = 1
BENCHMARK_RUNS_ARTIFACT_KIND = "libvt.benchmark.runs"
BENCHMARK_MEASUREMENT_ARTIFACT_KIND = "libvt.benchmark.measurement"
BENCHMARK_ANALYSIS_ARTIFACT_KIND = "libvt.benchmark.analysis"
BENCHMARK_ANALYSIS_COMMAND_ARTIFACT_KIND = "libvt.benchmark.analysis.command_summary"
BENCHMARK_ANALYSIS_WARNING_ARTIFACT_KIND = "libvt.benchmark.analysis.warning"
type CommandContext = dict[str, str]


@dataclass(frozen=True, slots=True)
class AnalysisCommandSummary:
    """Timing summary for one benchmark command across measurements.

    Attributes
    ----------
    command_name : str
        Configured benchmark entry key.
    measurement_count : int
        Number of commit measurements aggregated.
    sample_count : int
        Total raw timing samples across measurements.
    status_counts : dict[str, int]
        Count of measurements per status value.
    stats : dict[str, float | None]
        Selected percentile and summary statistics in seconds.
    """

    command_name: str
    measurement_count: int
    sample_count: int
    status_counts: dict[str, int]
    stats: dict[str, float | None]


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """Derived benchmark artifact analysis.

    Attributes
    ----------
    artifact_label : str
        Shareable filename label without local directory prefixes.
    command_summaries : tuple[AnalysisCommandSummary, ...]
        Per-command sample summaries.
    warnings : list[str]
        Diagnostic warnings emitted while summarizing rows.
    """

    artifact_label: str
    command_summaries: tuple[AnalysisCommandSummary, ...]
    warnings: list[str]


class BenchCommand(pydantic.BaseModel):
    """One configured benchmark command and its execution metadata."""

    model_config = pydantic.ConfigDict(extra="forbid")

    description: str = ""
    command: str
    default_query: str = ""
    timeout_seconds: int | None = pydantic.Field(default=None, ge=1)


class Settings(pydantic.BaseModel):
    """Global settings for the cross-commit benchmark runner."""

    model_config = pydantic.ConfigDict(extra="forbid")

    warmup: int = pydantic.Field(default=1, ge=0)
    runs: int = pydantic.Field(default=3, ge=1)
    trunk: str = "master"
    sync_command: str = "uv sync --quiet"
    venv: str = ".venv"
    timeout_seconds: int = pydantic.Field(default=300, ge=1)


class Config(pydantic.BaseModel):
    """Effective merged benchmark configuration."""

    model_config = pydantic.ConfigDict(extra="forbid")

    bench: dict[str, BenchCommand] = pydantic.Field(default_factory=dict)
    settings: Settings = pydantic.Field(default_factory=Settings)


def _bench_timeout_seconds(bench: BenchCommand, settings: Settings) -> int:
    """Return per-bench timeout if configured, else global timeout."""
    if bench.timeout_seconds is not None:
        return bench.timeout_seconds
    return settings.timeout_seconds


class Measurement(pydantic.BaseModel):
    """One ``(commit, bench)`` timing record."""

    schema_version: int = SCHEMA_VERSION
    artifact_kind: str = BENCHMARK_MEASUREMENT_ARTIFACT_KIND
    sha: str
    short_sha: str
    subject: str
    command_name: str
    command_string: str
    samples: list[float] = pydantic.Field(default_factory=list)
    status: Status = "ok"
    error: str | None = None
    dry_run: bool = False

    @property
    def avg_s(self) -> float:
        """Return arithmetic mean across samples in seconds."""
        if not self.samples:
            return float("nan")
        return sum(self.samples) / len(self.samples)


def percentile(sorted_vals: t.Sequence[float], pct: float) -> float:
    """Compute nearest-rank percentile on a pre-sorted sequence.

    Examples
    --------
    >>> percentile([1.0, 2.0, 3.0, 4.0], 50)
    2.0
    >>> percentile([1.0, 2.0, 3.0, 4.0], 100)
    4.0
    """
    if not sorted_vals:
        return float("nan")
    k = max(1, math.ceil(pct / 100.0 * len(sorted_vals)))
    return sorted_vals[min(k, len(sorted_vals)) - 1]


def stat_for_label(samples: t.Sequence[float], label: str) -> float:
    """Resolve ``min``, ``max``, ``avg``, or ``p<N>`` on *samples*."""
    if not samples:
        return float("nan")
    if label == "min":
        return min(samples)
    if label == "max":
        return max(samples)
    if label == "avg":
        return sum(samples) / len(samples)
    if label.startswith("p"):
        pct = float(label[1:])
        return percentile(sorted(samples), pct)
    msg = f"unknown stat label: {label!r}"
    raise ValueError(msg)


def _deep_merge(
    base: dict[str, t.Any],
    overlay: dict[str, t.Any],
) -> dict[str, t.Any]:
    """Recursively merge *overlay* onto a copy of *base*."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: pathlib.Path) -> dict[str, t.Any]:
    """Read a TOML file as a dict, returning ``{}`` when absent."""
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"failed to parse {path}: {exc}"
        raise typer.BadParameter(msg) from exc


def load_config(
    *,
    config_path: pathlib.Path | None = None,
    local_path: pathlib.Path | None = None,
    cli_overrides: dict[str, t.Any] | None = None,
) -> Config:
    """Load effective config from defaults, committed TOML, local TOML, and CLI."""
    layers: list[dict[str, t.Any]] = [
        {},
        _read_toml(config_path if config_path is not None else DEFAULT_CONFIG),
        _read_toml(local_path if local_path is not None else LOCAL_CONFIG),
    ]
    if cli_overrides:
        layers.append(cli_overrides)
    merged: dict[str, t.Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    try:
        return Config.model_validate(merged)
    except pydantic.ValidationError as exc:
        lines = [
            line
            for line in str(exc).splitlines()
            if not line.strip().startswith("For further information visit")
        ]
        msg = "invalid config:\n" + "\n".join(lines)
        raise typer.BadParameter(msg) from exc


def render_command(template: str, context: dict[str, str]) -> str:
    """Render a command template via ``str.format_map``."""
    return template.format_map(context)


def sanitize_command_string(
    command_string: str,
    context: CommandContext,
) -> str:
    """Replace local checkout paths with shareable template tokens."""
    replacements = {
        context.get("repo", ""): "{repo}",
        context.get("venv", ""): "{venv}",
        str(pathlib.Path.home()): "{home}",
    }
    query = context.get("query", "")
    if query:
        replacements[query] = "{query}"
    sanitized = command_string
    for raw, placeholder in sorted(
        replacements.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if raw:
            sanitized = sanitized.replace(raw, placeholder)
    return sanitized


class CommitRef(t.NamedTuple):
    """Identifying triple for a resolved git commit.

    Attributes
    ----------
    sha : str
        Full 40-character commit SHA.
    short_sha : str
        7-character abbreviated SHA.
    subject : str
        First line of the commit message.
    """

    sha: str
    short_sha: str
    subject: str


def _git(*args: str, repo: pathlib.Path = REPO_ROOT) -> str:
    """Run ``git <args>`` in *repo* and return stripped stdout."""
    proc = subprocess.run(
        ("git", *args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def resolve_target(
    *,
    target: str | None = None,
    range_spec: str | None = None,
    lookback: int | None = None,
    from_trunk_back: int | None = None,
    tags: bool = False,
    commits: str | None = None,
    head_vs_trunk: bool = False,
    trunk: str = "master",
    repo: pathlib.Path = REPO_ROOT,
    git_runner: t.Callable[[tuple[str, ...]], str] | None = None,
) -> list[CommitRef]:
    """Resolve a target selector to chronologically ordered commits."""

    def run(*args: str) -> str:
        if git_runner is not None:
            return git_runner(args).strip()
        return _git(*args, repo=repo)

    def commit_for(ref: str) -> CommitRef:
        full = run("rev-parse", ref)
        subject = run("log", "-1", "--pretty=format:%s", full)
        return CommitRef(sha=full, short_sha=full[:7], subject=subject)

    if head_vs_trunk:
        return [commit_for("HEAD"), commit_for(trunk)]
    if commits:
        return [commit_for(s.strip()) for s in commits.split(",") if s.strip()]
    if tags:
        out = run("tag", "--sort=v:refname")
        return [commit_for(line) for line in out.splitlines() if line.strip()]
    if range_spec:
        out = run("rev-list", "--reverse", range_spec)
        return [commit_for(line) for line in out.splitlines() if line.strip()]
    if lookback is not None:
        out = run("rev-list", "-n", str(lookback), "HEAD")
        shas = list(reversed([line for line in out.splitlines() if line.strip()]))
        return [commit_for(sha) for sha in shas]
    if from_trunk_back is not None:
        out = run("rev-list", "-n", str(from_trunk_back), trunk)
        shas = list(reversed([line for line in out.splitlines() if line.strip()]))
        return [commit_for(sha) for sha in shas]
    return [commit_for(target or "HEAD")]


def have_hyperfine() -> bool:
    """Return ``True`` when ``hyperfine`` is available on ``PATH``."""
    return shutil.which("hyperfine") is not None


def _time_with_hyperfine(
    cmd_str: str,
    *,
    warmup: int,
    runs: int,
    timeout_seconds: int,
) -> list[float]:
    """Run ``hyperfine -N`` and return raw sample durations in seconds."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        export_path = pathlib.Path(tmp.name)
    try:
        subprocess.run(
            (
                "hyperfine",
                "--warmup",
                str(warmup),
                "--runs",
                str(runs),
                "-N",
                "--export-json",
                str(export_path),
                cmd_str,
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        data = json.loads(export_path.read_text(encoding="utf-8"))
        return [float(x) for x in data["results"][0]["times"]]
    finally:
        export_path.unlink(missing_ok=True)


def _time_diy(
    cmd_str: str,
    *,
    warmup: int,
    runs: int,
    timeout_seconds: int,
) -> list[float]:
    """Measure command execution in pure Python when hyperfine is absent."""
    argv = shlex.split(cmd_str)
    for _ in range(warmup):
        subprocess.run(
            argv,
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(
            argv,
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        samples.append(time.perf_counter() - t0)
    return samples


def time_command(
    cmd_str: str,
    *,
    warmup: int,
    runs: int,
    timeout_seconds: int,
    prefer_hyperfine: bool = True,
) -> list[float]:
    """Time *cmd_str*, preferring ``hyperfine`` with a Python fallback."""
    if prefer_hyperfine and have_hyperfine():
        return _time_with_hyperfine(
            cmd_str,
            warmup=warmup,
            runs=runs,
            timeout_seconds=timeout_seconds,
        )
    return _time_diy(
        cmd_str,
        warmup=warmup,
        runs=runs,
        timeout_seconds=timeout_seconds,
    )


def _percentile_value(m: Measurement, label: str) -> float:
    if m.status != "ok":
        return float("nan")
    return stat_for_label(m.samples, label)


def _fmt_seconds(value: float) -> str:
    if math.isnan(value):
        return "-"
    return f"{value:.4f}s"


def render_rich(
    measurements: list[Measurement],
    percentile_labels: list[str],
) -> str:
    """Render benchmark measurements as Rich tables."""
    console = rich.console.Console(record=True, file=io.StringIO(), width=120)
    by_cmd: dict[str, list[Measurement]] = {}
    for m in measurements:
        by_cmd.setdefault(m.command_name, []).append(m)
    for cmd_name, rows in by_cmd.items():
        table = rich.table.Table(title=f"[bold]{cmd_name}[/bold]")
        table.add_column("sha", style="cyan")
        for label in percentile_labels:
            table.add_column(label, justify="right")
        table.add_column("status")
        table.add_column("subject", overflow="fold")
        for m in rows:
            table.add_row(
                m.short_sha,
                *(
                    _fmt_seconds(_percentile_value(m, label))
                    for label in percentile_labels
                ),
                m.status,
                m.subject[:80],
            )
        console.print(table)
    return console.export_text()


def render_json(measurements: list[Measurement], _labels: list[str]) -> str:
    """Render measurements as a single JSON artifact."""
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": BENCHMARK_RUNS_ARTIFACT_KIND,
            "runs": [m.model_dump(mode="json") for m in measurements],
        },
        indent=2,
    )


def render_ndjson(measurements: list[Measurement], _labels: list[str]) -> str:
    """Render measurements as newline-delimited JSON."""
    return "\n".join(json.dumps(m.model_dump(mode="json")) for m in measurements)


def _md_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_markdown(
    measurements: list[Measurement],
    percentile_labels: list[str],
) -> str:
    """Render measurements as Markdown tables."""
    out: list[str] = []
    by_cmd: dict[str, list[Measurement]] = {}
    for m in measurements:
        by_cmd.setdefault(m.command_name, []).append(m)
    for cmd_name, rows in by_cmd.items():
        out.append(f"## `{cmd_name}`\n")
        headers = ["sha", *percentile_labels, "status", "subject"]
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "|".join(["---"] * len(headers)) + "|")
        for m in rows:
            cells = [
                f"`{m.short_sha}`",
                *(
                    _fmt_seconds(_percentile_value(m, label))
                    for label in percentile_labels
                ),
                m.status,
                _md_escape(m.subject[:80]),
            ]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def render_csv(
    measurements: list[Measurement],
    percentile_labels: list[str],
) -> str:
    """Render measurements as CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "sha",
            "short_sha",
            "command",
            "status",
            *percentile_labels,
            "samples",
            "subject",
        ],
    )
    for m in measurements:
        writer.writerow(
            [
                m.sha,
                m.short_sha,
                m.command_name,
                m.status,
                *(f"{_percentile_value(m, label):.6f}" for label in percentile_labels),
                ";".join(f"{s:.6f}" for s in m.samples),
                m.subject,
            ],
        )
    return buffer.getvalue()


RENDERERS: dict[
    OutputFormat,
    t.Callable[[list[Measurement], list[str]], str],
] = {
    "rich": render_rich,
    "json": render_json,
    "ndjson": render_ndjson,
    "md": render_markdown,
    "csv": render_csv,
}


def load_measurement_artifact(path: pathlib.Path) -> list[Measurement]:
    """Load a saved JSON or NDJSON benchmark artifact."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"failed to read benchmark artifact {path}: {exc}"
        raise typer.BadParameter(msg) from exc
    if not text.strip():
        msg = f"unsupported benchmark artifact {path.name}: file is empty"
        raise typer.BadParameter(msg)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows = [
            Measurement.model_validate(json.loads(line))
            for line in text.splitlines()
            if line.strip()
        ]
        if rows:
            return rows
        msg = f"unsupported benchmark artifact {path.name}: no rows found"
        raise typer.BadParameter(msg) from None
    if isinstance(payload, dict) and isinstance(payload.get("runs"), list):
        return [Measurement.model_validate(row) for row in payload["runs"]]
    msg = f"unsupported benchmark artifact {path.name}"
    raise typer.BadParameter(msg)


def build_analysis_report(
    measurements: list[Measurement],
    *,
    artifact_label: str,
    percentile_labels: list[str],
) -> AnalysisReport:
    """Summarize loaded measurement rows by command."""
    by_command: dict[str, list[Measurement]] = {}
    for m in measurements:
        by_command.setdefault(m.command_name, []).append(m)
    summaries: list[AnalysisCommandSummary] = []
    for command_name, rows in by_command.items():
        status_counts: dict[str, int] = {}
        samples: list[float] = []
        for row in rows:
            status_counts[row.status] = status_counts.get(row.status, 0) + 1
            samples.extend(row.samples)
        stats = {
            label: (None if not samples else stat_for_label(samples, label))
            for label in percentile_labels
        }
        summaries.append(
            AnalysisCommandSummary(
                command_name=command_name,
                measurement_count=len(rows),
                sample_count=len(samples),
                status_counts=status_counts,
                stats=stats,
            ),
        )
    sampleless = sum(1 for m in measurements if not m.samples)
    warnings = [f"{sampleless} measurement(s) have no samples"] if sampleless else []
    return AnalysisReport(
        artifact_label=pathlib.Path(artifact_label).name or "benchmark",
        command_summaries=tuple(summaries),
        warnings=warnings,
    )


def _git_dirty(repo: pathlib.Path) -> bool:
    proc = subprocess.run(
        ("git", "diff-index", "--quiet", "HEAD", "--"),
        cwd=repo,
        check=False,
    )
    return proc.returncode != 0


def _checkout(sha: str, repo: pathlib.Path) -> None:
    subprocess.run(
        ("git", "checkout", "-q", sha),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _install_restore_guard(
    *,
    repo: pathlib.Path,
    original_ref: str,
    keep_checkout: bool,
) -> None:
    """Restore *original_ref* on normal exit, SIGINT, or SIGTERM."""
    if keep_checkout:
        return
    restored = {"done": False}

    def _restore() -> None:
        if restored["done"]:
            return
        restored["done"] = True
        with contextlib.suppress(OSError):
            subprocess.run(
                ("git", "checkout", "-q", original_ref),
                cwd=repo,
                check=False,
                capture_output=True,
            )

    atexit.register(_restore)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(
                sig,
                lambda _signo, _frame: (_restore(), sys.exit(130)),
            )
        except (ValueError, OSError):
            continue


app = typer.Typer(
    name="benchmark",
    help="Cross-commit benchmark harness for libvt.",
    add_completion=False,
    no_args_is_help=True,
)


def _parse_percentile_labels(raw: str) -> list[str]:
    labels = [piece.strip() for piece in raw.split(",") if piece.strip()]
    for label in labels:
        if label in ("min", "max", "avg"):
            continue
        if label.startswith("p") and label[1:].isdigit():
            continue
        msg = f"unknown stat label: {label!r}"
        raise typer.BadParameter(msg)
    return labels


def _select_bench_names(config: Config, commands: str | None) -> list[str]:
    if commands is None:
        return list(config.bench)
    names = [c.strip() for c in commands.split(",") if c.strip()]
    if not names:
        msg = "--commands did not select any benchmarks"
        raise typer.BadParameter(msg)
    missing = [n for n in names if n not in config.bench]
    if missing:
        msg = (
            f"unknown benchmark(s): {', '.join(missing)}; "
            f"available: {', '.join(config.bench)}"
        )
        raise typer.BadParameter(msg)
    return names


def _select_targets(
    *,
    target: str | None,
    range_spec: str | None,
    lookback: int | None,
    from_trunk_back: int | None,
    tags: bool,
    commits: str | None,
    head_vs_trunk: bool,
    trunk: str,
) -> list[CommitRef]:
    chosen = [
        bool(range_spec),
        lookback is not None,
        from_trunk_back is not None,
        tags,
        bool(commits),
        head_vs_trunk,
        target is not None and target != "HEAD",
    ]
    if sum(1 for c in chosen if c) > 1:
        msg = "target selectors are mutually exclusive"
        raise typer.BadParameter(msg)
    resolved_target = trunk if target == "trunk" else target
    try:
        return resolve_target(
            target=resolved_target,
            range_spec=range_spec,
            lookback=lookback,
            from_trunk_back=from_trunk_back,
            tags=tags,
            commits=commits,
            head_vs_trunk=head_vs_trunk,
            trunk=trunk,
        )
    except subprocess.CalledProcessError as exc:
        msg = f"git failed resolving target: {' '.join(exc.cmd)}"
        raise typer.BadParameter(msg) from exc


def _run_one_commit(
    *,
    commit: CommitRef,
    config: Config,
    bench_names: list[str],
    query_overrides: dict[str, str],
    runs: int,
    warmup: int,
    no_sync: bool,
    dry_run: bool,
    repo: pathlib.Path,
    prefer_hyperfine: bool,
    notify: t.Callable[[str], None],
) -> list[Measurement]:
    results: list[Measurement] = []
    try:
        _checkout(commit.sha, repo)
    except subprocess.CalledProcessError as exc:
        return [
            Measurement(
                sha=commit.sha,
                short_sha=commit.short_sha,
                subject=commit.subject,
                command_name=name,
                command_string=config.bench[name].command,
                status="checkout_fail",
                error=exc.stderr.strip() or "checkout failed",
                dry_run=dry_run,
            )
            for name in bench_names
        ]

    if not no_sync and not dry_run and config.settings.sync_command.strip():
        sync_proc = subprocess.run(
            shlex.split(config.settings.sync_command),
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if sync_proc.returncode != 0:
            err = (sync_proc.stderr or sync_proc.stdout or "sync failed").strip()
            return [
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=config.bench[name].command,
                    status="sync_fail",
                    error=err[:300],
                    dry_run=dry_run,
                )
                for name in bench_names
            ]

    venv = (repo / config.settings.venv).resolve()
    for name in bench_names:
        bench = config.bench[name]
        context = {
            "venv": str(venv),
            "query": query_overrides.get(name, bench.default_query),
            "sha": commit.sha,
            "short_sha": commit.short_sha,
            "repo": str(repo),
        }
        cmd_str = render_command(bench.command, context)
        sanitized = sanitize_command_string(cmd_str, context)
        if dry_run:
            notify(f"[{commit.short_sha}] {name}: {cmd_str} (dry-run)")
            results.append(
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=sanitized,
                    status="ok",
                    dry_run=True,
                ),
            )
            continue
        notify(f"[{commit.short_sha}] {name}")
        try:
            samples = time_command(
                cmd_str,
                warmup=warmup,
                runs=runs,
                timeout_seconds=_bench_timeout_seconds(bench, config.settings),
                prefer_hyperfine=prefer_hyperfine,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            results.append(
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=sanitized,
                    status="bench_fail",
                    error=str(exc),
                ),
            )
            continue
        results.append(
            Measurement(
                sha=commit.sha,
                short_sha=commit.short_sha,
                subject=commit.subject,
                command_name=name,
                command_string=sanitized,
                samples=samples,
                status="ok",
            ),
        )
    return results


@app.command("run")
def cmd_run(
    target: str = typer.Option("HEAD", "--target"),
    range_spec: str | None = typer.Option(None, "--range"),
    lookback: int | None = typer.Option(None, "--lookback"),
    from_trunk_back: int | None = typer.Option(None, "--from-trunk-back"),
    tags: bool = typer.Option(False, "--tags"),
    commits: str | None = typer.Option(None, "--commits"),
    head_vs_trunk: bool = typer.Option(False, "--head-vs-trunk"),
    config_path: pathlib.Path | None = typer.Option(None, "--config"),
    commands: str | None = typer.Option(None, "--commands"),
    runs: int | None = typer.Option(None, "--runs"),
    warmup: int | None = typer.Option(None, "--warmup"),
    query: str | None = typer.Option(None, "--query"),
    no_sync: bool = typer.Option(False, "--no-sync"),
    keep_checkout: bool = typer.Option(False, "--keep-checkout"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
    output_format: OutputFormat = typer.Option("rich", "--format"),
    output: pathlib.Path | None = typer.Option(None, "--output"),
    show_percentiles: str = typer.Option(
        "min,avg,p50,p90,p95,p99,max",
        "--show-percentiles",
    ),
    no_progress: bool = typer.Option(False, "--no-progress"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_hyperfine: bool = typer.Option(False, "--no-hyperfine"),
) -> None:
    """Run configured benchmarks across resolved git commits."""
    settings_override: dict[str, t.Any] = {}
    if runs is not None:
        settings_override["runs"] = runs
    if warmup is not None:
        settings_override["warmup"] = warmup
    cli_overrides = {"settings": settings_override} if settings_override else None
    config = load_config(config_path=config_path, cli_overrides=cli_overrides)
    if not config.bench:
        typer.echo("error: no [bench.*] entries configured.", err=True)
        raise typer.Exit(code=2)
    bench_names = _select_bench_names(config, commands)
    percentile_labels = _parse_percentile_labels(show_percentiles)
    query_overrides = dict.fromkeys(bench_names, query) if query is not None else {}
    if not allow_dirty and _git_dirty(REPO_ROOT):
        typer.echo(
            "error: uncommitted changes present; commit or pass --allow-dirty.",
            err=True,
        )
        raise typer.Exit(code=2)
    targets = _select_targets(
        target=target,
        range_spec=range_spec,
        lookback=lookback,
        from_trunk_back=from_trunk_back,
        tags=tags,
        commits=commits,
        head_vs_trunk=head_vs_trunk,
        trunk=config.settings.trunk,
    )
    original_ref = _git("rev-parse", "--abbrev-ref", "HEAD", repo=REPO_ROOT)
    if original_ref == "HEAD":
        original_ref = _git("rev-parse", "HEAD", repo=REPO_ROOT)
    _install_restore_guard(
        repo=REPO_ROOT,
        original_ref=original_ref,
        keep_checkout=keep_checkout,
    )

    def notify(line: str) -> None:
        if not no_progress:
            typer.echo(line, err=True)

    measurements: list[Measurement] = []
    for commit in targets:
        measurements.extend(
            _run_one_commit(
                commit=commit,
                config=config,
                bench_names=bench_names,
                query_overrides=query_overrides,
                runs=config.settings.runs,
                warmup=config.settings.warmup,
                no_sync=no_sync,
                dry_run=dry_run,
                repo=REPO_ROOT,
                prefer_hyperfine=not no_hyperfine,
                notify=notify,
            ),
        )
    rendered = RENDERERS[output_format](measurements, percentile_labels)
    if output is not None:
        output.write_text(rendered, encoding="utf-8")
    else:
        typer.echo(rendered)


@app.command("compare")
def cmd_compare(
    a: str = typer.Argument(...),
    b: str = typer.Argument(...),
    config_path: pathlib.Path | None = typer.Option(None, "--config"),
    commands: str | None = typer.Option(None, "--commands"),
    runs: int | None = typer.Option(None, "--runs"),
    warmup: int | None = typer.Option(None, "--warmup"),
    output_format: OutputFormat = typer.Option("rich", "--format"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_hyperfine: bool = typer.Option(False, "--no-hyperfine"),
) -> None:
    """Compare two git refs side by side."""
    cmd_run(
        target="HEAD",
        range_spec=None,
        lookback=None,
        from_trunk_back=None,
        tags=False,
        commits=f"{a},{b}",
        head_vs_trunk=False,
        config_path=config_path,
        commands=commands,
        runs=runs,
        warmup=warmup,
        query=None,
        no_sync=False,
        keep_checkout=False,
        allow_dirty=allow_dirty,
        output_format=output_format,
        output=None,
        show_percentiles="min,avg,p50,p90,p95,p99,max",
        no_progress=False,
        dry_run=dry_run,
        no_hyperfine=no_hyperfine,
    )


@app.command("analyze")
def cmd_analyze(
    artifact: pathlib.Path = typer.Argument(...),
    output_format: AnalysisOutputFormat = typer.Option("rich", "--format"),
    show_percentiles: str = typer.Option(
        "min,avg,p50,p90,p95,p99,max",
        "--show-percentiles",
    ),
) -> None:
    """Summarize a saved JSON or NDJSON benchmark artifact."""
    labels = _parse_percentile_labels(show_percentiles)
    measurements = load_measurement_artifact(artifact)
    report = build_analysis_report(
        measurements,
        artifact_label=artifact.name,
        percentile_labels=labels,
    )
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_kind": BENCHMARK_ANALYSIS_ARTIFACT_KIND,
                    "artifact_label": report.artifact_label,
                    "command_summaries": [asdict(s) for s in report.command_summaries],
                    "warnings": report.warnings,
                },
                indent=2,
            ),
        )
        return
    if output_format == "ndjson":
        rows: list[dict[str, object]] = [
            {
                "schema_version": SCHEMA_VERSION,
                "artifact_kind": BENCHMARK_ANALYSIS_COMMAND_ARTIFACT_KIND,
                "artifact_label": report.artifact_label,
                **asdict(s),
            }
            for s in report.command_summaries
        ]
        rows.extend(
            {
                "schema_version": SCHEMA_VERSION,
                "artifact_kind": BENCHMARK_ANALYSIS_WARNING_ARTIFACT_KIND,
                "artifact_label": report.artifact_label,
                "message": w,
            }
            for w in report.warnings
        )
        typer.echo("\n".join(json.dumps(r) for r in rows))
        return
    console = rich.console.Console(record=True, file=io.StringIO(), width=120)
    table = rich.table.Table(title=f"analysis: {report.artifact_label}")
    table.add_column("command")
    table.add_column("samples", justify="right")
    for label in labels:
        table.add_column(label, justify="right")
    for summary in report.command_summaries:
        table.add_row(
            summary.command_name,
            str(summary.sample_count),
            *(
                _fmt_seconds(val) if val is not None else "-"
                for val in summary.stats.values()
            ),
        )
    console.print(table)
    typer.echo(console.export_text())


@app.command("list-commits")
def cmd_list_commits(
    target: str = typer.Option("HEAD", "--target"),
    range_spec: str | None = typer.Option(None, "--range"),
    lookback: int | None = typer.Option(None, "--lookback"),
    from_trunk_back: int | None = typer.Option(None, "--from-trunk-back"),
    tags: bool = typer.Option(False, "--tags"),
    commits: str | None = typer.Option(None, "--commits"),
    head_vs_trunk: bool = typer.Option(False, "--head-vs-trunk"),
    config_path: pathlib.Path | None = typer.Option(None, "--config"),
) -> None:
    """List commits resolved by a target selector without running benchmarks."""
    config = load_config(config_path=config_path)
    for c in _select_targets(
        target=target,
        range_spec=range_spec,
        lookback=lookback,
        from_trunk_back=from_trunk_back,
        tags=tags,
        commits=commits,
        head_vs_trunk=head_vs_trunk,
        trunk=config.settings.trunk,
    ):
        typer.echo(f"{c.short_sha}  {c.subject}")


@app.command("list-commands")
def cmd_list_commands(
    config_path: pathlib.Path | None = typer.Option(None, "--config"),
) -> None:
    """Print configured benchmark entries after TOML layering."""
    config = load_config(config_path=config_path)
    for name, bench in config.bench.items():
        typer.echo(f"{name}: {bench.command}")


@app.command("show-config")
def cmd_show_config(
    config_path: pathlib.Path | None = typer.Option(None, "--config"),
) -> None:
    """Dump the effective merged configuration as JSON."""
    config = load_config(config_path=config_path)
    typer.echo(json.dumps(config.model_dump(mode="json"), indent=2))


def main(argv: list[str] | None = None) -> int:
    """Dispatch CLI arguments to the Typer application."""
    try:
        result = app(args=argv, standalone_mode=False)
    except click.exceptions.UsageError as exc:
        typer.echo(f"error: {exc.format_message()}", err=True)
        return 2
    except typer.BadParameter as exc:
        typer.echo(f"error: {exc}", err=True)
        return 2
    if isinstance(result, int):
        return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
