"""Data quality tools for earnings events.

Background
----------
Some upstream sources (Finnhub free-tier surprise endpoint, yfinance for
recent quarters) return the fiscal *period-end* date instead of the actual
*announcement* date. Symptom: hundreds of tickers appear to "report" on
the same day, almost always a calendar quarter-end (Mar 31, Jun 30,
Sep 30, Dec 31).

This module:
- Audits the earnings_events table for suspicious date concentration
- Cleans (deletes) rows on fake quarter-end dates and their downstream
  realized_moves
- Provides a regression check that lives in the test suite forever

The proper fix — sourcing announcement dates from SEC 8-K item 2.02
filings — lives in `earnings_from_edgar.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)

# Calendar quarter-end month/day pairs in US fiscal reporting
QUARTER_END_PAIRS: frozenset[tuple[int, int]] = frozenset({(3, 31), (6, 30), (9, 30), (12, 31)})

# A date is "suspicious" if N or more tickers share it. Calibrated against
# the diagnostic output: real busy reporting days top out around ~35-40 of
# 250 large caps; fake quarter-end rows show 200+ tickers same day.
DEFAULT_CONCENTRATION_THRESHOLD = 50


@dataclass(frozen=True)
class SuspiciousDate:
    """A date flagged as a likely fiscal-period-end placeholder."""

    event_date: date
    n_tickers: int
    is_calendar_quarter_end: bool


@dataclass(frozen=True)
class AuditResult:
    """Output of the audit pass."""

    total_events: int
    n_suspicious_dates: int
    n_suspicious_events: int
    suspicious: list[SuspiciousDate]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit_earnings_dates(
    conn: duckdb.DuckDBPyConnection,
    *,
    concentration_threshold: int = DEFAULT_CONCENTRATION_THRESHOLD,
) -> AuditResult:
    """Identify event_dates with suspicious ticker concentration.

    Returns the full audit result. Does not modify the warehouse.

    A date is flagged when BOTH conditions hold:
    - >= ``concentration_threshold`` distinct tickers share it
    - It is a calendar quarter-end (Mar 31 / Jun 30 / Sep 30 / Dec 31)

    Real busy reporting days do hit 30-40 large caps on the same day, but
    they almost never land on a calendar quarter-end. The combined filter
    avoids false positives.
    """
    rows = conn.execute(
        """
        SELECT event_date, COUNT(DISTINCT ticker) AS n_tickers
        FROM earnings_events
        WHERE eps_actual IS NOT NULL
        GROUP BY event_date
        HAVING COUNT(DISTINCT ticker) >= ?
        ORDER BY n_tickers DESC
        """,
        [concentration_threshold],
    ).fetchall()

    suspicious: list[SuspiciousDate] = []
    for event_date, n in rows:
        is_qe = (event_date.month, event_date.day) in QUARTER_END_PAIRS
        if is_qe:
            suspicious.append(
                SuspiciousDate(
                    event_date=event_date, n_tickers=int(n), is_calendar_quarter_end=True
                )
            )

    total_events = conn.execute(
        "SELECT COUNT(*) FROM earnings_events WHERE eps_actual IS NOT NULL"
    ).fetchone()
    total = int(total_events[0]) if total_events else 0

    n_susp_events = sum(s.n_tickers for s in suspicious)

    log.info(
        "earnings_audit",
        total_events=total,
        n_suspicious_dates=len(suspicious),
        n_suspicious_events=n_susp_events,
    )
    return AuditResult(
        total_events=total,
        n_suspicious_dates=len(suspicious),
        n_suspicious_events=n_susp_events,
        suspicious=suspicious,
    )


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


def clean_fake_earnings_dates(
    conn: duckdb.DuckDBPyConnection,
    *,
    concentration_threshold: int = DEFAULT_CONCENTRATION_THRESHOLD,
    dry_run: bool = True,
) -> tuple[int, int]:
    """Delete suspicious rows from earnings_events and dependent realized_moves.

    Returns (n_earnings_deleted, n_realized_moves_deleted).
    With dry_run=True (default), returns the counts but performs no deletes.
    """
    audit = audit_earnings_dates(conn, concentration_threshold=concentration_threshold)
    if not audit.suspicious:
        log.info("earnings_clean_noop")
        return (0, 0)

    bad_dates = [s.event_date for s in audit.suspicious]
    placeholders = ",".join("?" * len(bad_dates))

    n_earnings = conn.execute(
        f"""
        SELECT COUNT(*) FROM earnings_events
        WHERE event_date IN ({placeholders})
        """,
        bad_dates,
    ).fetchone()
    n_earnings_count = int(n_earnings[0]) if n_earnings else 0

    n_moves = conn.execute(
        f"""
        SELECT COUNT(*) FROM realized_moves
        WHERE event_date IN ({placeholders})
        """,
        bad_dates,
    ).fetchone()
    n_moves_count = int(n_moves[0]) if n_moves else 0

    if dry_run:
        log.info(
            "earnings_clean_dry_run",
            would_delete_earnings=n_earnings_count,
            would_delete_realized_moves=n_moves_count,
            bad_dates=[str(d) for d in bad_dates],
        )
        return (n_earnings_count, n_moves_count)

    # Live delete
    conn.execute(
        f"DELETE FROM realized_moves WHERE event_date IN ({placeholders})",
        bad_dates,
    )
    conn.execute(
        f"DELETE FROM earnings_events WHERE event_date IN ({placeholders})",
        bad_dates,
    )
    log.info(
        "earnings_cleaned",
        earnings_deleted=n_earnings_count,
        realized_moves_deleted=n_moves_count,
    )
    return (n_earnings_count, n_moves_count)


# ---------------------------------------------------------------------------
# Regression check — for the test suite
# ---------------------------------------------------------------------------


def assert_no_date_concentration(
    conn: duckdb.DuckDBPyConnection,
    *,
    concentration_threshold: int = DEFAULT_CONCENTRATION_THRESHOLD,
) -> None:
    """Raise AssertionError if any quarter-end day has >= threshold tickers.

    Designed to be called from a test that runs against the live warehouse.
    See tests/test_earnings_quality.py.
    """
    audit = audit_earnings_dates(conn, concentration_threshold=concentration_threshold)
    if audit.suspicious:
        msg = (
            f"Found {len(audit.suspicious)} suspicious quarter-end dates with "
            f">= {concentration_threshold} tickers. Likely fiscal period-end "
            f"placeholder data. Run `catalyst data clean-earnings`. "
            f"Offending dates: {[(str(s.event_date), s.n_tickers) for s in audit.suspicious]}"
        )
        raise AssertionError(msg)
