"""Finnhub earnings ingestion.

Two endpoints we use
--------------------
1. Earnings calendar:
       GET /calendar/earnings?from=YYYY-MM-DD&to=YYYY-MM-DD
   Returns every scheduled US earnings release in the window with estimates.
   This is the "what's coming" feed that drives the dashboard.

2. Per-ticker surprise history:
       GET /stock/earnings?symbol=AAPL
   Returns up to ~20 quarters of {period, actual, estimate, surprise, ...}.
   This is the label data for the backtest — every historical event we
   score against.

Rate limiting
-------------
Free tier: 60 calls/minute. We target 50 calls/minute (12 req/sec ceiling
on burst, but smoothed) to leave headroom.

Point-in-time
-------------
- For ACTUALS: `as_of = event_date`. The actual EPS is observable from the
  moment of the press release. We don't have intraday precision on this
  from Finnhub, so we round to the event date.
- For ESTIMATES: `as_of = ingestion time`. Finnhub doesn't expose a full
  estimate revision history on the free tier, so the best we can claim is
  "this was the consensus at the time we observed it." Documented in
  data_dictionary.md as a known limitation.

When the same (ticker, event_date) gets re-ingested with a different
estimate or actual, we write a NEW row with a new `as_of`, never overwrite.
That's how the warehouse stays honest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from catalyst_engine.config import get_settings
from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"


@dataclass(frozen=True)
class EarningsEvent:
    """An earnings event ready for warehouse insert.

    Either `eps_actual` is None (upcoming event with estimate only) or it is
    populated (historical event).
    """

    ticker: str
    event_date: date
    time_of_day: str  # BMO | AMC | DMH | UNK
    fiscal_period: str | None
    eps_est: float | None
    eps_actual: float | None
    revenue_est: float | None
    revenue_actual: float | None
    as_of: datetime  # see module docstring for semantics


class FinnhubRateLimiter:
    """Smooth rate limiter targeting 50 calls/minute (≈ 1.2s between calls)."""

    def __init__(self, calls_per_minute: int = 50) -> None:
        self._min_interval = 60.0 / calls_per_minute
        self._last_request_time = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()


def _build_client(api_key: str | None = None) -> httpx.Client:
    """Construct an HTTPX client with the Finnhub auth header."""
    key = api_key or get_settings().finnhub_api_key
    if not key:
        raise ValueError(
            "FINNHUB_API_KEY is empty. Set it in .env "
            "(get a free key at https://finnhub.io/register)."
        )
    return httpx.Client(
        timeout=30.0,
        base_url=FINNHUB_BASE,
        headers={"X-Finnhub-Token": key},
    )


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def _get_json(
    client: httpx.Client, path: str, params: dict[str, Any], limiter: FinnhubRateLimiter
) -> dict[str, Any]:
    limiter.wait()
    resp = client.get(path, params=params)
    if resp.status_code == 429:
        # Rate limit. Sleep longer and let tenacity retry.
        log.warning("finnhub_rate_limited", path=path, sleeping_s=10)
        time.sleep(10)
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


# Finnhub `hour` values: "bmo", "amc", "dmh", or "" / null
_TIME_OF_DAY_MAP = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}


def _parse_time_of_day(raw: str | None) -> str:
    if not raw:
        return "UNK"
    return _TIME_OF_DAY_MAP.get(raw.lower(), "UNK")


def _fiscal_period(quarter: int | None, year: int | None) -> str | None:
    if quarter is None or year is None:
        return None
    return f"Q{quarter} {year}"


def _coerce_float(value: Any) -> float | None:
    """Finnhub sometimes returns 0 / null / string. Normalize to float | None."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # Finnhub uses 0 as a sentinel for "no estimate" in some calendar payloads.
    # We keep 0 as 0 (it's a legitimate value for low-cap names) and treat
    # only None/empty as missing. Downstream code can filter as needed.
    return f


# ---------------------------------------------------------------------------
# Calendar endpoint
# ---------------------------------------------------------------------------


def fetch_calendar(
    client: httpx.Client,
    limiter: FinnhubRateLimiter,
    *,
    start: date,
    end: date,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch the earnings calendar for [start, end].

    If `symbol` is set, restricts to that ticker. Returns the raw list of
    earningsCalendar entries (parsing happens in parse_calendar_response).
    """
    params: dict[str, Any] = {"from": start.isoformat(), "to": end.isoformat()}
    if symbol:
        params["symbol"] = symbol
    payload = _get_json(client, "/calendar/earnings", params, limiter)
    entries: list[dict[str, Any]] = payload.get("earningsCalendar") or []
    log.debug("finnhub_calendar_fetched", start=str(start), end=str(end), n=len(entries))
    return entries


def parse_calendar_response(
    entries: list[dict[str, Any]],
    *,
    as_of: datetime,
    universe_tickers: set[str] | None = None,
) -> list[EarningsEvent]:
    """Convert raw calendar entries into EarningsEvent records.

    If `universe_tickers` is provided, entries outside the set are dropped
    (Finnhub returns the full US calendar by default).
    """
    out: list[EarningsEvent] = []
    for raw in entries:
        ticker = (raw.get("symbol") or "").upper()
        if not ticker:
            continue
        if universe_tickers is not None and ticker not in universe_tickers:
            continue

        event_date_str = raw.get("date")
        if not event_date_str:
            continue
        try:
            event_date = date.fromisoformat(event_date_str)
        except ValueError:
            continue

        eps_actual = _coerce_float(raw.get("epsActual"))

        # If this row already has an actual, use the event date as as_of
        # (the print was observable from that day). Otherwise, this is a
        # forward-looking estimate, as_of is when we observed it.
        row_as_of = (
            datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc)
            if eps_actual is not None
            else as_of
        )

        out.append(
            EarningsEvent(
                ticker=ticker,
                event_date=event_date,
                time_of_day=_parse_time_of_day(raw.get("hour")),
                fiscal_period=_fiscal_period(raw.get("quarter"), raw.get("year")),
                eps_est=_coerce_float(raw.get("epsEstimate")),
                eps_actual=eps_actual,
                revenue_est=_coerce_float(raw.get("revenueEstimate")),
                revenue_actual=_coerce_float(raw.get("revenueActual")),
                as_of=row_as_of,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Per-ticker surprise history endpoint
# ---------------------------------------------------------------------------


def fetch_surprise_history(
    client: httpx.Client,
    limiter: FinnhubRateLimiter,
    *,
    ticker: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` quarters of historical earnings surprises.

    Returns the raw list. Parsing happens in parse_surprise_response.
    """
    payload = _get_json(
        client,
        "/stock/earnings",
        {"symbol": ticker, "limit": limit},
        limiter,
    )
    # The earnings surprise endpoint returns a bare list, not a wrapper object.
    if isinstance(payload, list):
        entries = payload
    else:
        entries = payload.get("earnings", []) if isinstance(payload, dict) else []
    log.debug("finnhub_surprises_fetched", ticker=ticker, n=len(entries))
    return entries


def parse_surprise_response(
    entries: list[dict[str, Any]],
    *,
    ticker: str,
) -> list[EarningsEvent]:
    """Convert surprise history entries into EarningsEvent records.

    For historical actuals, as_of = event_date (00:00 UTC). The actual was
    public from the print moment; we lose intraday precision here but that's
    acceptable for backtest purposes — features computed at event_date - 1
    will see only the prior estimate, not the actual.
    """
    out: list[EarningsEvent] = []
    for raw in entries:
        period_str = raw.get("period")
        if not period_str:
            continue
        try:
            event_date = date.fromisoformat(period_str)
        except ValueError:
            continue

        actual = _coerce_float(raw.get("actual"))
        estimate = _coerce_float(raw.get("estimate"))

        as_of = datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc)

        out.append(
            EarningsEvent(
                ticker=ticker.upper(),
                event_date=event_date,
                time_of_day="UNK",  # surprise endpoint doesn't expose BMO/AMC
                fiscal_period=_fiscal_period(raw.get("quarter"), raw.get("year")),
                eps_est=estimate,
                eps_actual=actual,
                revenue_est=None,  # not in this endpoint
                revenue_actual=None,
                as_of=as_of,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Warehouse upsert
# ---------------------------------------------------------------------------


def upsert_earnings_events(
    conn: duckdb.DuckDBPyConnection, records: list[EarningsEvent]
) -> int:
    """Insert records. Idempotent on (ticker, event_date, as_of).

    Returns rows actually written. Same logical event with a later `as_of`
    creates a new row, preserving estimate-revision history. That's
    intentional — see methodology.md.

    Storage convention: DuckDB TIMESTAMP columns store naive timestamps.
    We accept tz-aware `as_of` on input (the EarningsEvent contract) and
    normalize to UTC-naive on the wire. Reads come back tz-naive; the
    convention "all warehouse times are UTC" is documented and enforced
    in src/catalyst_engine/utils/pit.py for PIT queries.
    """
    if not records:
        return 0

    def _to_storage(ts: datetime) -> datetime:
        """Tz-aware → UTC-naive for DuckDB TIMESTAMP storage."""
        if ts.tzinfo is None:
            return ts
        return ts.astimezone(timezone.utc).replace(tzinfo=None)

    # Dedupe within batch on the storage key
    seen: set[tuple[str, date, datetime]] = set()
    deduped: list[tuple[EarningsEvent, datetime]] = []
    for r in records:
        storage_as_of = _to_storage(r.as_of)
        key = (r.ticker, r.event_date, storage_as_of)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((r, storage_as_of))

    # Find which rows already exist
    placeholders = ",".join("(?, ?, ?)" for _ in deduped)
    existing_rows = conn.execute(
        f"""
        SELECT ticker, event_date, as_of FROM earnings_events
        WHERE (ticker, event_date, as_of) IN ({placeholders})
        """,
        [v for r, ts in deduped for v in (r.ticker, r.event_date, ts)],
    ).fetchall()
    existing = {(row[0], row[1], row[2]) for row in existing_rows}

    to_insert = [
        (r, ts) for r, ts in deduped if (r.ticker, r.event_date, ts) not in existing
    ]
    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO earnings_events (
            ticker, event_date, time_of_day, fiscal_period,
            eps_est, eps_actual, revenue_est, revenue_actual,
            as_of, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'finnhub')
        """,
        [
            (
                r.ticker,
                r.event_date,
                r.time_of_day,
                r.fiscal_period,
                r.eps_est,
                r.eps_actual,
                r.revenue_est,
                r.revenue_actual,
                ts,
            )
            for r, ts in to_insert
        ],
    )
    log.info(
        "earnings_upserted",
        new=len(to_insert),
        skipped=len(deduped) - len(to_insert),
    )
    return len(to_insert)


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------


def ingest_calendar_window(
    conn: duckdb.DuckDBPyConnection,
    *,
    start: date,
    end: date,
    universe_tickers: set[str] | None = None,
    chunk_days: int = 30,
) -> int:
    """Ingest the earnings calendar over a date range.

    Finnhub's calendar endpoint can be slow for large windows. We chunk into
    `chunk_days` segments to keep individual calls fast and to make retries
    cheap on transient errors.

    Returns total new rows written.
    """
    limiter = FinnhubRateLimiter()
    now = datetime.now(timezone.utc)
    total_new = 0

    with _build_client() as client:
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            entries = fetch_calendar(client, limiter, start=cursor, end=chunk_end)
            records = parse_calendar_response(
                entries, as_of=now, universe_tickers=universe_tickers
            )
            total_new += upsert_earnings_events(conn, records)
            cursor = chunk_end + timedelta(days=1)

    log.info(
        "earnings_calendar_done",
        start=str(start),
        end=str(end),
        new_rows=total_new,
    )
    return total_new


def ingest_surprise_history(
    conn: duckdb.DuckDBPyConnection,
    *,
    tickers: list[str],
    limit_per_ticker: int = 20,
) -> dict[str, int]:
    """Pull historical earnings surprises for each ticker.

    Returns {ticker: rows_written}. Tickers that fail are recorded with -1.
    """
    limiter = FinnhubRateLimiter()
    results: dict[str, int] = {}

    with _build_client() as client:
        for ticker in tickers:
            try:
                entries = fetch_surprise_history(
                    client, limiter, ticker=ticker, limit=limit_per_ticker
                )
                records = parse_surprise_response(entries, ticker=ticker)
                results[ticker] = upsert_earnings_events(conn, records)
            except httpx.HTTPError as exc:
                log.error("surprise_history_failed", ticker=ticker, error=str(exc))
                results[ticker] = -1

    successes = sum(1 for v in results.values() if v >= 0)
    failures = sum(1 for v in results.values() if v < 0)
    total = sum(v for v in results.values() if v > 0)
    log.info(
        "surprise_history_done",
        tickers=len(results),
        successes=successes,
        failures=failures,
        total_rows=total,
    )
    return results
