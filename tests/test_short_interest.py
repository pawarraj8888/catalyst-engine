"""Tests for the FINRA short interest ingestion module."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb

from catalyst_engine.data.short_interest import (
    ShortInterestSnapshot,
    upsert_short_interest,
)

UTC = UTC


def _snap(ticker: str, settle: date, si: int) -> ShortInterestSnapshot:
    return ShortInterestSnapshot(
        ticker=ticker,
        settlement_date=settle,
        short_interest=si,
        avg_daily_volume=1_000_000,
        days_to_cover=si / 1_000_000,
        as_of=datetime.now(UTC),
    )


def test_upsert_writes_new_rows(warehouse: duckdb.DuckDBPyConnection) -> None:
    snaps = [
        _snap("AAPL", date(2025, 1, 15), 100_000_000),
        _snap("MSFT", date(2025, 1, 15), 50_000_000),
    ]
    n = upsert_short_interest(warehouse, snaps)
    assert n == 2

    total = warehouse.execute("SELECT COUNT(*) FROM short_interest").fetchone()
    assert total[0] == 2


def test_upsert_is_idempotent(warehouse: duckdb.DuckDBPyConnection) -> None:
    snaps = [_snap("AAPL", date(2025, 1, 15), 100_000_000)]
    assert upsert_short_interest(warehouse, snaps) == 1
    assert upsert_short_interest(warehouse, snaps) == 0  # second call no-ops


def test_upsert_handles_empty(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert upsert_short_interest(warehouse, []) == 0


def test_upsert_writes_dtc(warehouse: duckdb.DuckDBPyConnection) -> None:
    snaps = [_snap("AAPL", date(2025, 1, 15), 5_000_000)]
    upsert_short_interest(warehouse, snaps)
    row = warehouse.execute(
        "SELECT days_to_cover FROM short_interest WHERE ticker = 'AAPL'"
    ).fetchone()
    assert row is not None
    assert float(row[0]) == 5.0


def test_candidate_settlement_dates_ordered() -> None:
    """Most recent candidate first, all <= today."""
    from datetime import date

    from catalyst_engine.data.short_interest import candidate_settlement_dates

    today = date(2026, 5, 27)
    candidates = candidate_settlement_dates(today)
    assert all(d <= today for d in candidates)
    assert candidates == sorted(candidates, reverse=True)
    assert len(candidates) >= 4


def test_candidate_settlement_dates_includes_mid_and_eom() -> None:
    """For a typical date, both mid-month and end-of-month show up."""
    from datetime import date

    from catalyst_engine.data.short_interest import candidate_settlement_dates

    today = date(2026, 5, 27)
    candidates = candidate_settlement_dates(today)
    # May 15, 2026 is a Friday
    assert date(2026, 5, 15) in candidates
    # April 30, 2026 is a Thursday — last business day of April
    assert date(2026, 4, 30) in candidates


def test_candidate_settlement_dates_skips_weekends() -> None:
    """If the 15th lands on a weekend, the prior business day is used."""
    from datetime import date

    from catalyst_engine.data.short_interest import candidate_settlement_dates

    # In Feb 2026: Feb 15 is a Sunday
    today = date(2026, 2, 27)
    candidates = candidate_settlement_dates(today)
    # Should use Feb 13 (Friday), not Feb 15 (Sunday)
    assert date(2026, 2, 13) in candidates
    assert date(2026, 2, 15) not in candidates
