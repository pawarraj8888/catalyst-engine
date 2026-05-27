"""Rebuild earnings dates from EDGAR 8-K item 2.02 filings.

Background
----------
SEC Form 8-K item 2.02 ("Results of Operations and Financial Condition")
is the official channel for earnings releases. The filing's `filed_at`
timestamp is the announcement moment. This is the gold-standard source
for announcement dates.

We already ingested 8-K filings in Phase 1 (~6,000 of them across the
universe). This module extracts those with item 2.02 and uses them as
the authoritative source for `earnings_events.event_date`.

How it works
------------
1. SELECT * FROM filings WHERE filing_type='8-K' AND '2.02' IN items
2. For each filing: ticker + filed_at -> a (ticker, announcement_date) row
3. Either UPSERT new earnings_events for these announcements, or UPDATE
   existing rows that point to fiscal-period-ends.

Limitations
-----------
- We have ~90 days of 8-Ks per ticker in V1 — not 5 years. So this fixes
  recent quarters but doesn't rebuild deep history (which had correct
  Finnhub calendar dates already and survives the audit anyway).
- 8-K item 2.02 doesn't always cleanly map to "earnings release" — some
  companies file 2.02 for guidance updates between earnings. We accept
  this noise; the dates we get are still real announcement dates of
  material financial events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


@dataclass(frozen=True)
class EdgarAnnouncement:
    """An earnings announcement extracted from an 8-K item 2.02 filing."""

    ticker: str
    cik: str
    announcement_date: date
    accession_number: str
    filed_at: datetime


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_earnings_announcements(
    conn: duckdb.DuckDBPyConnection,
) -> list[EdgarAnnouncement]:
    """Pull every 8-K item 2.02 from the filings table.

    Returns one record per filing. A ticker may appear multiple times if
    multiple 8-K item 2.02s were filed (e.g., earnings + pre-announcement).
    """
    rows = conn.execute("""
        SELECT ticker, cik, accession_number, filed_at, items
        FROM filings
        WHERE filing_type = '8-K'
          AND ticker IS NOT NULL
          AND items IS NOT NULL
          AND list_contains(items, '2.02')
        ORDER BY ticker, filed_at
        """).fetchall()

    out: list[EdgarAnnouncement] = []
    for ticker, cik, acc, filed_at, _items in rows:
        # filed_at can come back tz-naive (DuckDB strips tz on storage)
        if filed_at.tzinfo is None:
            filed_at_utc = filed_at.replace(tzinfo=UTC)
        else:
            filed_at_utc = filed_at.astimezone(UTC)
        out.append(
            EdgarAnnouncement(
                ticker=ticker,
                cik=cik,
                announcement_date=filed_at_utc.date(),
                accession_number=acc,
                filed_at=filed_at_utc,
            )
        )

    log.info("edgar_announcements_extracted", n=len(out))
    return out


# ---------------------------------------------------------------------------
# Match existing earnings_events rows to EDGAR announcements
# ---------------------------------------------------------------------------


def match_announcements_to_fiscal_periods(
    conn: duckdb.DuckDBPyConnection,
    announcements: list[EdgarAnnouncement],
    *,
    max_days_after_period: int = 90,
) -> list[tuple[EdgarAnnouncement, date | None]]:
    """For each announcement, find the fiscal period it most likely covers.

    The fiscal period is identified as the most recent (ticker, event_date)
    in earnings_events that:
      - has event_date <= announcement_date
      - is no more than ``max_days_after_period`` before the announcement
      - has eps_actual set (it's a historical reported event)

    Returns a list of (announcement, matched_fiscal_period or None).
    Unmatched announcements get None — they may be ahead of any fiscal
    period we know about (e.g., a 2026 announcement when our calendar
    only had 2025 historical data).
    """
    out: list[tuple[EdgarAnnouncement, date | None]] = []
    for ann in announcements:
        row = conn.execute(
            """
            SELECT event_date
            FROM earnings_events
            WHERE ticker = ?
              AND event_date <= ?
              AND event_date >= ? - INTERVAL '90 days'
              AND eps_actual IS NOT NULL
            ORDER BY event_date DESC
            LIMIT 1
            """,
            [ann.ticker, ann.announcement_date, ann.announcement_date],
        ).fetchone()
        matched: date | None = row[0] if row else None
        out.append((ann, matched))

    n_matched = sum(1 for _, m in out if m is not None)
    log.info(
        "announcements_matched",
        total=len(announcements),
        matched=n_matched,
        unmatched=len(announcements) - n_matched,
    )
    return out


# ---------------------------------------------------------------------------
# Rewrite earnings_events with announcement dates
# ---------------------------------------------------------------------------


def rewrite_event_dates(
    conn: duckdb.DuckDBPyConnection,
    matched: list[tuple[EdgarAnnouncement, date | None]],
    *,
    dry_run: bool = True,
) -> tuple[int, int]:
    """For each matched (announcement, fiscal_period), insert a new
    earnings_events row stamped with the announcement_date.

    The original row is left in place — we don't delete history. After
    this runs, downstream code (realized_moves) should be re-pointed at
    only events with announcement_date semantics.

    To prevent ambiguity, this writes a new row with ``source='edgar'``.
    The audit / clean pass can then drop the period-end rows safely.

    Returns (n_to_insert, n_inserted). With dry_run, second is 0.
    """
    candidates: list[EdgarAnnouncement] = [ann for ann, m in matched if m is not None]
    n_candidates = len(candidates)

    if not candidates:
        return (0, 0)

    if dry_run:
        log.info("rewrite_event_dates_dry_run", n_would_insert=n_candidates)
        return (n_candidates, 0)

    # Dedupe: one (ticker, announcement_date) at most
    seen: set[tuple[str, date]] = set()
    unique: list[EdgarAnnouncement] = []
    for ann in candidates:
        key = (ann.ticker, ann.announcement_date)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ann)

    # Skip rows that would conflict with existing
    placeholders = ",".join("(?, ?)" for _ in unique)
    existing_rows = conn.execute(
        f"""
        SELECT ticker, event_date FROM earnings_events
        WHERE (ticker, event_date) IN ({placeholders}) AND source = 'edgar'
        """,
        [v for ann in unique for v in (ann.ticker, ann.announcement_date)],
    ).fetchall()
    existing = {(r[0], r[1]) for r in existing_rows}
    to_insert = [ann for ann in unique if (ann.ticker, ann.announcement_date) not in existing]
    if not to_insert:
        return (n_candidates, 0)

    conn.executemany(
        """
        INSERT INTO earnings_events (
            ticker, event_date, time_of_day, fiscal_period,
            eps_est, eps_actual, revenue_est, revenue_actual,
            as_of, source
        ) VALUES (?, ?, 'UNK', NULL, NULL, 1.0, NULL, NULL, ?, 'edgar')
        """,
        [
            (
                ann.ticker,
                ann.announcement_date,
                ann.filed_at.replace(tzinfo=None),
            )
            for ann in to_insert
        ],
    )
    log.info(
        "edgar_event_dates_inserted",
        candidates=n_candidates,
        inserted=len(to_insert),
        skipped_existing=len(existing),
    )
    return (n_candidates, len(to_insert))


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


def rebuild_from_edgar(
    conn: duckdb.DuckDBPyConnection,
    *,
    dry_run: bool = True,
    require_fiscal_match: bool = False,
) -> dict[str, int]:
    """Run the full rebuild pipeline.

    When ``require_fiscal_match=False`` (default), every 8-K item 2.02
    becomes its own earnings_events row even if we can't match it to an
    existing fiscal period. This is the right behavior when fiscal-period
    coverage in earnings_events is sparse (e.g. after a cleanup pass).

    When ``require_fiscal_match=True``, only announcements that match a
    known fiscal period get written. This is conservative — useful when
    you want to enrich an existing complete history with announcement
    dates rather than create new events.

    Returns a stats dict: {announcements, matched, candidates, inserted}.
    """
    announcements = extract_earnings_announcements(conn)
    matched = match_announcements_to_fiscal_periods(conn, announcements)

    if require_fiscal_match:
        n_cand, n_inserted = rewrite_event_dates(conn, matched, dry_run=dry_run)
    else:
        # Insert ALL announcements as standalone earnings events
        all_pairs: list[tuple[EdgarAnnouncement, date | None]] = [
            (ann, ann.announcement_date) for ann in announcements
        ]
        n_cand, n_inserted = rewrite_event_dates(conn, all_pairs, dry_run=dry_run)

    return {
        "announcements": len(announcements),
        "matched": sum(1 for _, m in matched if m is not None),
        "candidates": n_cand,
        "inserted": n_inserted,
    }
