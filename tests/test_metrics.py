"""Tests for backtest metrics — bucketing, hit rate, bootstrap CI."""

from __future__ import annotations

import uuid

import duckdb

from catalyst_engine.backtest.metrics import (
    _bootstrap_ci,
    compute_bucket_metrics,
    compute_summary,
)

# ---------------------------------------------------------------------------
# Bootstrap CI primitive
# ---------------------------------------------------------------------------


def test_bootstrap_ci_empty_sample() -> None:
    assert _bootstrap_ci(0, 0) == (0.0, 0.0)


def test_bootstrap_ci_all_successes() -> None:
    """100/100 successes - CI should be tight near 1.0."""
    lo, hi = _bootstrap_ci(100, 100)
    assert lo >= 0.95
    assert hi == 1.0


def test_bootstrap_ci_all_failures() -> None:
    lo, hi = _bootstrap_ci(0, 100)
    assert lo == 0.0
    assert hi <= 0.05


def test_bootstrap_ci_50pct() -> None:
    """50/100 should give a CI roughly centered on 0.5."""
    lo, hi = _bootstrap_ci(50, 100, n_resamples=2000)
    assert 0.35 < lo < 0.50
    assert 0.50 < hi < 0.65


# ---------------------------------------------------------------------------
# Bucket + summary computation against a synthetic warehouse
# ---------------------------------------------------------------------------


def _insert_scored(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    score: float,
    is_hit: bool | None,
    ticker: str = "X",
    event_year: int = 2024,
) -> None:
    import json

    conn.execute(
        """
        INSERT INTO scored_setups (
            ticker, event_date, catalyst_type, score, rules_fired,
            score_components, is_hit, score_as_of, run_id
        ) VALUES (?, ?, 'earnings', ?, ?, ?, ?, ?, ?)
        """,
        [
            ticker,
            f"{event_year}-01-01",
            score,
            [],
            json.dumps({}),
            is_hit,
            f"{event_year}-01-01 00:00:00",
            run_id,
        ],
    )


def test_compute_bucket_metrics_basic(warehouse: duckdb.DuckDBPyConnection) -> None:
    run_id = str(uuid.uuid4())
    # Bucket 0-2: 5 hits / 10 -> 50%
    for i in range(10):
        _insert_scored(
            warehouse,
            run_id=run_id,
            score=1.0,
            is_hit=(i < 5),
            ticker=f"A{i}",
            event_year=2020 + i,
        )
    # Bucket 6-8: 7 hits / 10 -> 70%
    for i in range(10):
        _insert_scored(
            warehouse,
            run_id=run_id,
            score=7.0,
            is_hit=(i < 7),
            ticker=f"B{i}",
            event_year=2020 + i,
        )

    buckets = compute_bucket_metrics(warehouse, run_id=run_id)
    by_label = {b.bucket_label: b for b in buckets}

    assert by_label["0-2"].n_setups == 10
    assert by_label["0-2"].n_hits == 5
    assert abs(by_label["0-2"].hit_rate - 0.5) < 1e-9

    assert by_label["6-8"].n_setups == 10
    assert by_label["6-8"].n_hits == 7
    assert abs(by_label["6-8"].hit_rate - 0.7) < 1e-9

    # Untouched bucket
    assert by_label["8-10"].n_setups == 0


def test_compute_bucket_metrics_excludes_null_hits(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Setups where is_hit is NULL (no label available) must not count."""
    run_id = str(uuid.uuid4())
    _insert_scored(warehouse, run_id=run_id, score=5.0, is_hit=True, ticker="A", event_year=2020)
    _insert_scored(warehouse, run_id=run_id, score=5.0, is_hit=None, ticker="B", event_year=2021)
    _insert_scored(warehouse, run_id=run_id, score=5.0, is_hit=False, ticker="C", event_year=2022)

    buckets = compute_bucket_metrics(warehouse, run_id=run_id)
    bucket_4_6 = next(b for b in buckets if b.bucket_label == "4-6")
    assert bucket_4_6.n_setups == 2  # NULL excluded
    assert bucket_4_6.n_hits == 1


def test_compute_summary_overall(warehouse: duckdb.DuckDBPyConnection) -> None:
    run_id = str(uuid.uuid4())
    for i in range(8):
        _insert_scored(
            warehouse,
            run_id=run_id,
            score=5.0,
            is_hit=(i < 5),
            ticker=f"T{i}",
            event_year=2020 + i,
        )

    summary = compute_summary(warehouse, run_id=run_id)
    assert summary.n_setups == 8
    assert summary.n_evaluable == 8
    assert abs(summary.overall_hit_rate - 5 / 8) < 1e-9


def test_compute_summary_isolated_per_run(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Setups from other runs do not pollute this run's metrics."""
    run_id = str(uuid.uuid4())
    other_run = str(uuid.uuid4())
    _insert_scored(warehouse, run_id=run_id, score=5.0, is_hit=True, ticker="X", event_year=2020)
    _insert_scored(
        warehouse, run_id=other_run, score=5.0, is_hit=False, ticker="Y", event_year=2021
    )

    summary = compute_summary(warehouse, run_id=run_id)
    assert summary.n_setups == 1
    assert summary.overall_hit_rate == 1.0
