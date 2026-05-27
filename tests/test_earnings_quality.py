"""Tests for earnings data quality tools."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import pytest

from catalyst_engine.data.earnings_from_edgar import (
    extract_earnings_announcements,
    match_announcements_to_fiscal_periods,
    rebuild_from_edgar,
    rewrite_event_dates,
)
from catalyst_engine.data.earnings_quality import (
    QUARTER_END_PAIRS,
    SuspiciousDate,
    assert_no_date_concentration,
    audit_earnings_dates,
    clean_fake_earnings_dates,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _insert_earnings(
    conn: duckdb.DuckDBPyConnection,
    rows: list[tuple[str, date]],
    *,
    source: str = "test",
    eps_actual: float | None = 1.0,
) -> None:
    conn.executemany(
        """
        INSERT INTO earnings_events
        (ticker, event_date, time_of_day, eps_est, eps_actual,
         revenue_est, revenue_actual, as_of, source)
        VALUES (?, ?, 'UNK', NULL, ?, NULL, NULL, ?, ?)
        """,
        [(t, d, eps_actual, datetime(d.year, d.month, d.day), source) for t, d in rows],
    )


def _insert_realized_move(conn: duckdb.DuckDBPyConnection, ticker: str, event_date: date) -> None:
    conn.execute(
        """
        INSERT INTO realized_moves
        (ticker, event_date, abs_move_1d, n_prior_events, as_of)
        VALUES (?, ?, 0.05, 0, ?)
        """,
        [ticker, event_date, datetime(event_date.year, event_date.month, event_date.day)],
    )


def _insert_filing(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    cik: str,
    accession: str,
    filed_at: datetime,
    items: list[str],
) -> None:
    conn.execute(
        """
        INSERT INTO filings
        (accession_number, cik, ticker, filing_type, filed_at, items, as_of, source)
        VALUES (?, ?, ?, '8-K', ?, ?, ?, 'edgar')
        """,
        [accession, cik, ticker, filed_at, items, filed_at],
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_flags_quarter_end_concentration(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Many tickers on a calendar quarter-end -> flagged."""
    bad_date = date(2025, 12, 31)
    rows = [(f"T{i:03d}", bad_date) for i in range(60)]
    _insert_earnings(warehouse, rows)

    result = audit_earnings_dates(warehouse, concentration_threshold=50)
    assert len(result.suspicious) == 1
    assert result.suspicious[0].event_date == bad_date
    assert result.suspicious[0].n_tickers == 60
    assert result.suspicious[0].is_calendar_quarter_end is True


def test_audit_does_not_flag_real_busy_day(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """A real heavy reporting day (~35 tickers) on a non-quarter-end date
    is NOT flagged, even at threshold 30."""
    # Pick a Wednesday well clear of any quarter-end
    busy_day = date(2024, 5, 1)
    assert (busy_day.month, busy_day.day) not in QUARTER_END_PAIRS

    rows = [(f"R{i:03d}", busy_day) for i in range(35)]
    _insert_earnings(warehouse, rows)

    result = audit_earnings_dates(warehouse, concentration_threshold=30)
    assert result.suspicious == []  # not a quarter-end, so not flagged


def test_audit_does_not_flag_low_count_on_quarter_end(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """A quarter-end date with only a few tickers is legitimate
    (some companies do have fiscal years that end Dec 31 and report ON Dec 31
    of an off-cycle period). Don't flag unless concentration > threshold.
    """
    qe_date = date(2025, 12, 31)
    rows = [(f"T{i}", qe_date) for i in range(5)]
    _insert_earnings(warehouse, rows)

    result = audit_earnings_dates(warehouse, concentration_threshold=50)
    assert result.suspicious == []


def test_audit_only_counts_actual_earnings(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Upcoming events (eps_actual IS NULL) shouldn't trigger the audit."""
    rows = [(f"T{i:03d}", date(2026, 3, 31)) for i in range(60)]
    _insert_earnings(warehouse, rows, eps_actual=None)

    result = audit_earnings_dates(warehouse, concentration_threshold=50)
    assert result.suspicious == []


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


def test_clean_dry_run_does_not_delete(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    bad_date = date(2025, 12, 31)
    rows = [(f"T{i:03d}", bad_date) for i in range(60)]
    _insert_earnings(warehouse, rows)

    n_earn, n_moves = clean_fake_earnings_dates(warehouse, concentration_threshold=50, dry_run=True)
    assert n_earn == 60
    assert n_moves == 0  # no realized_moves rows in this fixture

    # Verify nothing actually deleted
    count = warehouse.execute("SELECT COUNT(*) FROM earnings_events").fetchone()
    assert count is not None and count[0] == 60


def test_clean_deletes_when_not_dry_run(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    bad_date = date(2025, 12, 31)
    good_date = date(2024, 5, 1)

    _insert_earnings(warehouse, [(f"BAD{i:03d}", bad_date) for i in range(60)])
    _insert_earnings(warehouse, [(f"OK{i:03d}", good_date) for i in range(20)])

    n_earn, _ = clean_fake_earnings_dates(warehouse, concentration_threshold=50, dry_run=False)
    assert n_earn == 60

    remaining = warehouse.execute("SELECT COUNT(*) FROM earnings_events").fetchone()
    assert remaining is not None and remaining[0] == 20  # good ones survive


def test_clean_cascades_to_realized_moves(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """When earnings rows are cleaned, their downstream realized_moves go too."""
    bad_date = date(2025, 12, 31)
    _insert_earnings(warehouse, [(f"T{i:03d}", bad_date) for i in range(60)])
    for i in range(60):
        _insert_realized_move(warehouse, f"T{i:03d}", bad_date)

    n_earn, n_moves = clean_fake_earnings_dates(
        warehouse, concentration_threshold=50, dry_run=False
    )
    assert n_earn == 60
    assert n_moves == 60

    rm_count = warehouse.execute("SELECT COUNT(*) FROM realized_moves").fetchone()
    assert rm_count is not None and rm_count[0] == 0


def test_clean_noop_when_clean(warehouse: duckdb.DuckDBPyConnection) -> None:
    _insert_earnings(warehouse, [("AAPL", date(2024, 5, 1))])
    n_earn, n_moves = clean_fake_earnings_dates(warehouse, dry_run=False)
    assert (n_earn, n_moves) == (0, 0)


# ---------------------------------------------------------------------------
# Regression check — this is the forever-guard
# ---------------------------------------------------------------------------


def test_assert_no_date_concentration_passes_on_clean(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    _insert_earnings(warehouse, [("AAPL", date(2024, 5, 1))])
    assert_no_date_concentration(warehouse)  # no exception


def test_assert_no_date_concentration_fails_on_dirty(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    _insert_earnings(warehouse, [(f"T{i:03d}", date(2025, 12, 31)) for i in range(60)])
    with pytest.raises(AssertionError, match="suspicious quarter-end dates"):
        assert_no_date_concentration(warehouse)


# ---------------------------------------------------------------------------
# EDGAR-based rebuild
# ---------------------------------------------------------------------------


def test_extract_announcements_picks_up_2_02(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Only 8-Ks containing item 2.02 are returned."""
    _insert_filing(
        warehouse,
        ticker="AAPL",
        cik="0000320193",
        accession="acc1",
        filed_at=datetime(2025, 8, 1, 16, 30, tzinfo=UTC),
        items=["2.02", "9.01"],
    )
    _insert_filing(
        warehouse,
        ticker="AAPL",
        cik="0000320193",
        accession="acc2",
        filed_at=datetime(2025, 9, 5, 17, 0, tzinfo=UTC),
        items=["5.02"],  # officer change — not an earnings event
    )

    announcements = extract_earnings_announcements(warehouse)
    assert len(announcements) == 1
    assert announcements[0].ticker == "AAPL"
    assert announcements[0].announcement_date == date(2025, 8, 1)
    assert announcements[0].accession_number == "acc1"


def test_match_announcements_to_fiscal_periods(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """An announcement on Aug 1 should match a fiscal period ending Jun 30
    (within 90 days). One ending in Mar should NOT match it."""
    _insert_earnings(
        warehouse,
        [("AAPL", date(2025, 6, 30)), ("AAPL", date(2025, 3, 31))],
    )
    _insert_filing(
        warehouse,
        ticker="AAPL",
        cik="0000320193",
        accession="acc1",
        filed_at=datetime(2025, 8, 1, 16, 30, tzinfo=UTC),
        items=["2.02", "9.01"],
    )

    announcements = extract_earnings_announcements(warehouse)
    matched = match_announcements_to_fiscal_periods(warehouse, announcements)
    assert len(matched) == 1
    _, period = matched[0]
    assert period == date(2025, 6, 30)  # most recent within 90d


def test_rewrite_event_dates_dry_run(warehouse: duckdb.DuckDBPyConnection) -> None:
    _insert_earnings(warehouse, [("AAPL", date(2025, 6, 30))])
    _insert_filing(
        warehouse,
        ticker="AAPL",
        cik="0000320193",
        accession="acc1",
        filed_at=datetime(2025, 8, 1, 16, 30, tzinfo=UTC),
        items=["2.02", "9.01"],
    )

    announcements = extract_earnings_announcements(warehouse)
    matched = match_announcements_to_fiscal_periods(warehouse, announcements)

    n_cand, n_ins = rewrite_event_dates(warehouse, matched, dry_run=True)
    assert n_cand == 1
    assert n_ins == 0

    # Original row preserved, no new edgar-sourced row
    count = warehouse.execute(
        "SELECT COUNT(*) FROM earnings_events WHERE source = 'edgar'"
    ).fetchone()
    assert count is not None and count[0] == 0


def test_rewrite_event_dates_live(warehouse: duckdb.DuckDBPyConnection) -> None:
    _insert_earnings(warehouse, [("AAPL", date(2025, 6, 30))])
    _insert_filing(
        warehouse,
        ticker="AAPL",
        cik="0000320193",
        accession="acc1",
        filed_at=datetime(2025, 8, 1, 16, 30, tzinfo=UTC),
        items=["2.02", "9.01"],
    )

    announcements = extract_earnings_announcements(warehouse)
    matched = match_announcements_to_fiscal_periods(warehouse, announcements)

    n_cand, n_ins = rewrite_event_dates(warehouse, matched, dry_run=False)
    assert n_cand == 1
    assert n_ins == 1

    # New row exists with source='edgar' and date = announcement date
    row = warehouse.execute(
        """
        SELECT event_date, source FROM earnings_events
        WHERE ticker = 'AAPL' AND source = 'edgar'
        """
    ).fetchone()
    assert row is not None
    assert row[0] == date(2025, 8, 1)


def test_rebuild_from_edgar_orchestration(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """End-to-end: filings -> matched -> rewritten. Dry run."""
    _insert_earnings(warehouse, [("MSFT", date(2025, 6, 30))])
    _insert_filing(
        warehouse,
        ticker="MSFT",
        cik="0000789019",
        accession="ms1",
        filed_at=datetime(2025, 7, 25, 16, 30, tzinfo=UTC),
        items=["2.02"],
    )

    stats = rebuild_from_edgar(warehouse, dry_run=True)
    assert stats["announcements"] == 1
    assert stats["matched"] == 1
    assert stats["candidates"] == 1
    assert stats["inserted"] == 0


# ---------------------------------------------------------------------------
# SuspiciousDate dataclass shape
# ---------------------------------------------------------------------------


def test_suspicious_date_dataclass() -> None:
    s = SuspiciousDate(event_date=date(2025, 12, 31), n_tickers=200, is_calendar_quarter_end=True)
    assert s.event_date == date(2025, 12, 31)
    assert s.n_tickers == 200
