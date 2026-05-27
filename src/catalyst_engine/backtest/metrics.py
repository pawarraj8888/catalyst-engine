"""Backtest metrics.

Given a `run_id` from a replay, compute:
- Hit rate by score bucket
- Bootstrap 95% CI per bucket (1000 resamples)
- Overall hit rate, sample size, coverage

We're explicit about what hit means: `abs_move_1d > trailing_median_8q`.
That's the size-of-move proxy. Directional accuracy requires positioning
or skew features which arrive in Phase 2.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


@dataclass(frozen=True)
class BucketMetrics:
    """Metrics for one score bucket."""

    bucket_label: str
    score_min: float
    score_max: float
    n_setups: int
    n_hits: int
    hit_rate: float
    ci_low_95: float
    ci_high_95: float


@dataclass(frozen=True)
class BacktestSummary:
    """Top-level output of metrics computation."""

    run_id: str
    n_setups: int
    n_evaluable: int  # has both score and is_hit non-null
    overall_hit_rate: float
    buckets: list[BucketMetrics]


# ---------------------------------------------------------------------------
# Bootstrap CI for a proportion
# ---------------------------------------------------------------------------


def _bootstrap_ci(
    successes: int,
    n: int,
    *,
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float]:
    """Bootstrap a 95% CI for a binomial proportion.

    Empty samples return (0, 0). When all are hits or all are misses, the
    bootstrap degenerates correctly (every resample = same proportion).
    """
    if n == 0:
        return (0.0, 0.0)

    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(n_resamples):
        wins = sum(1 for _ in range(n) if rng.random() < (successes / n))
        samples.append(wins / n)
    samples.sort()
    lo = samples[int(n_resamples * alpha / 2)]
    hi = samples[int(n_resamples * (1 - alpha / 2))]
    return (lo, hi)


# ---------------------------------------------------------------------------
# Bucket computation
# ---------------------------------------------------------------------------


DEFAULT_BUCKETS: list[tuple[str, float, float]] = [
    ("0-2", 0.0, 2.0),
    ("2-4", 2.0, 4.0),
    ("4-6", 4.0, 6.0),
    ("6-8", 6.0, 8.0),
    ("8-10", 8.0, 10.01),
]


def compute_bucket_metrics(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    buckets: list[tuple[str, float, float]] | None = None,
) -> list[BucketMetrics]:
    """Compute hit rates per score bucket for a given run."""
    if buckets is None:
        buckets = DEFAULT_BUCKETS

    out: list[BucketMetrics] = []
    for label, lo, hi in buckets:
        row = conn.execute(
            """
            SELECT COUNT(*), SUM(CAST(is_hit AS INTEGER))
            FROM scored_setups
            WHERE run_id = ? AND score >= ? AND score < ?
              AND is_hit IS NOT NULL
            """,
            [run_id, lo, hi],
        ).fetchone()
        n = int(row[0]) if row and row[0] else 0
        hits = int(row[1]) if row and row[1] else 0
        rate = hits / n if n > 0 else 0.0
        ci_lo, ci_hi = _bootstrap_ci(hits, n)
        out.append(
            BucketMetrics(
                bucket_label=label,
                score_min=lo,
                score_max=hi,
                n_setups=n,
                n_hits=hits,
                hit_rate=rate,
                ci_low_95=ci_lo,
                ci_high_95=ci_hi,
            )
        )
    return out


def compute_summary(conn: duckdb.DuckDBPyConnection, *, run_id: str) -> BacktestSummary:
    """Top-level summary for a backtest run."""
    total = conn.execute("SELECT COUNT(*) FROM scored_setups WHERE run_id = ?", [run_id]).fetchone()
    n_setups = int(total[0]) if total and total[0] else 0

    evaluable_row = conn.execute(
        """
        SELECT COUNT(*), SUM(CAST(is_hit AS INTEGER))
        FROM scored_setups
        WHERE run_id = ? AND is_hit IS NOT NULL
        """,
        [run_id],
    ).fetchone()
    n_eval = int(evaluable_row[0]) if evaluable_row and evaluable_row[0] else 0
    n_hits = int(evaluable_row[1]) if evaluable_row and evaluable_row[1] else 0
    overall = n_hits / n_eval if n_eval > 0 else 0.0

    buckets = compute_bucket_metrics(conn, run_id=run_id)

    log.info(
        "backtest_summary_computed",
        run_id=run_id,
        n_setups=n_setups,
        n_eval=n_eval,
        overall_hit_rate=overall,
    )
    return BacktestSummary(
        run_id=run_id,
        n_setups=n_setups,
        n_evaluable=n_eval,
        overall_hit_rate=overall,
        buckets=buckets,
    )
