"""FINRA short interest ingestion.

Source
------
FINRA publishes short interest data bi-monthly (settlement on the 15th
and last business day of each month). Free, no auth.

The structured endpoint we use:
    https://api.finra.org/data/group/otcMarket/name/equityShortInterest

For each (ticker, settlement_date) we want:
- short_interest (currentShortShareNumber)
- avg_daily_volume (averageDailyShareVolume)
- days_to_cover (daysToCover)

FINRA quirk
-----------
This dataset is *partitioned by settlementDate*. The API refuses to
sort across partitions, so we can't ask "give me the most recent SI for
AAPL" with one call. The right pattern is:

1. Figure out the most likely recent settlement date (the 15th or last
   business day of the current or previous month).
2. Query the API by that exact settlementDate, pulling the full universe
   in one or a few calls.

In V0 we try the most recent two candidate settlement dates and take
whatever comes back. Time-series for z-score computation accumulates
over weeks.

Data quality notes
------------------
- Free FINRA endpoint; no auth.
- Some smaller / international tickers won't be in FINRA data; they get
  silently skipped (logged at debug level).
- FINRA returns rows in chunks (default 1000 per response). We paginate
  via `offset` until exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)

FINRA_BASE_URL = "https://api.finra.org/data/group/otcMarket/name/equityShortInterest"
PAGE_SIZE = 1000


@dataclass(frozen=True)
class ShortInterestSnapshot:
    """One short interest record for a (ticker, settlement_date)."""

    ticker: str
    settlement_date: date
    short_interest: int | None
    avg_daily_volume: int | None
    days_to_cover: float | None
    as_of: datetime


# ---------------------------------------------------------------------------
# Settlement-date heuristic
# ---------------------------------------------------------------------------


def _last_business_day_of_month(year: int, month: int) -> date:
    """Return the last business day of the given month."""
    # Step forward to next month, then back one day
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    d = first_of_next - timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def candidate_settlement_dates(today: date) -> list[date]:
    """Generate plausible recent settlement dates in descending order.

    FINRA reports SI bi-monthly: settlement on the 15th of each month
    and the last business day. There's usually a ~10 day reporting lag
    before the data is published.

    We return the last ~4 candidates so that if today's right after a
    new release, or right before, we still hit one with data.
    """
    candidates: list[date] = []
    for months_back in range(3):
        target = today.replace(day=1)
        for _ in range(months_back):
            target = target - timedelta(days=1)
            target = target.replace(day=1)
        # Mid-month settlement
        mid = target.replace(day=15)
        while mid.weekday() >= 5:
            mid -= timedelta(days=1)
        # End-of-month settlement
        eom = _last_business_day_of_month(target.year, target.month)
        candidates.extend([eom, mid])
    # Dedupe while preserving order, drop any future dates
    seen: set[date] = set()
    unique: list[date] = []
    for d in candidates:
        if d <= today and d not in seen:
            seen.add(d)
            unique.append(d)
    return sorted(unique, reverse=True)


# ---------------------------------------------------------------------------
# Fetch (partition-aware)
# ---------------------------------------------------------------------------


def _fetch_partition(client: httpx.Client, settlement_date: date) -> list[dict[str, Any]]:
    """Fetch ALL rows for one settlement_date, paginating until exhausted."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    all_rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = {
            "compareFilters": [
                {
                    "compareType": "EQUAL",
                    "fieldName": "settlementDate",
                    "fieldValue": settlement_date.isoformat(),
                }
            ],
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        try:
            resp = client.post(FINRA_BASE_URL, json=payload, headers=headers, timeout=60.0)
            resp.raise_for_status()
        except Exception as exc:
            log.debug("finra_partition_fetch_error", date=str(settlement_date), error=str(exc))
            break

        rows = resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        if offset > 50_000:  # safety cap
            log.warning("finra_pagination_runaway", offset=offset)
            break
    return all_rows


def fetch_short_interest_for_universe(
    tickers: list[str], *, today: date | None = None
) -> list[ShortInterestSnapshot]:
    """Fetch the most recent SI snapshot covering the given tickers.

    Tries candidate settlement dates in reverse chronological order until
    one returns a non-empty universe. Filters results down to our tickers.
    """
    today_d = today or datetime.now(UTC).date()
    target_tickers = {t.upper() for t in tickers}

    snapshots: list[ShortInterestSnapshot] = []
    now_utc = datetime.now(UTC)
    with httpx.Client() as client:
        for candidate in candidate_settlement_dates(today_d):
            rows = _fetch_partition(client, candidate)
            log.info("finra_partition_fetched", date=str(candidate), n_rows=len(rows))
            if not rows:
                continue

            kept = 0
            for row in rows:
                sym = (row.get("issueSymbolIdentifier") or "").upper()
                if sym not in target_tickers:
                    continue
                try:
                    settle = datetime.strptime(row["settlementDate"], "%Y-%m-%d").date()
                except (KeyError, ValueError):
                    continue
                short_int_raw = row.get("currentShortShareNumber")
                adv_raw = row.get("averageDailyShareVolume")
                dtc_raw = row.get("daysToCover")

                def _to_float(x: Any) -> float | None:
                    if x is None:
                        return None
                    try:
                        return float(x)
                    except (TypeError, ValueError):
                        return None

                short_int = _to_float(short_int_raw)
                adv = _to_float(adv_raw)
                dtc = _to_float(dtc_raw)
                if dtc is None and short_int and adv and adv > 0:
                    dtc = short_int / adv

                snapshots.append(
                    ShortInterestSnapshot(
                        ticker=sym,
                        settlement_date=settle,
                        short_interest=int(short_int) if short_int is not None else None,
                        avg_daily_volume=int(adv) if adv is not None else None,
                        days_to_cover=dtc,
                        as_of=now_utc,
                    )
                )
                kept += 1
            log.info("finra_partition_matched", date=str(candidate), kept=kept)
            if kept > 0:
                # First partition with data wins; stop here
                break
    return snapshots


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def upsert_short_interest(
    conn: duckdb.DuckDBPyConnection, snapshots: list[ShortInterestSnapshot]
) -> int:
    """Insert SI snapshots; idempotent on (ticker, settlement_date)."""
    if not snapshots:
        return 0

    placeholders = ",".join("(?, ?)" for _ in snapshots)
    existing_rows = conn.execute(
        f"""
        SELECT ticker, settlement_date FROM short_interest
        WHERE (ticker, settlement_date) IN ({placeholders})
        """,
        [v for s in snapshots for v in (s.ticker, s.settlement_date)],
    ).fetchall()
    existing = {(r[0], r[1]) for r in existing_rows}

    to_insert = [s for s in snapshots if (s.ticker, s.settlement_date) not in existing]
    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO short_interest
        (ticker, settlement_date, short_interest, avg_daily_volume,
         days_to_cover, shares_outstanding, pct_float, as_of, source)
        VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'finra')
        """,
        [
            (
                s.ticker,
                s.settlement_date,
                s.short_interest,
                s.avg_daily_volume,
                s.days_to_cover,
                s.as_of.replace(tzinfo=None),
            )
            for s in to_insert
        ],
    )
    log.info("short_interest_upserted", new=len(to_insert), skipped=len(existing))
    return len(to_insert)


def ingest_universe(conn: duckdb.DuckDBPyConnection, tickers: list[str]) -> int:
    """End-to-end: fetch + upsert. Returns rows written."""
    snapshots = fetch_short_interest_for_universe(tickers)
    return upsert_short_interest(conn, snapshots)
