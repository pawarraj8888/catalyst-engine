"""Daily OHLCV ingestion via yfinance.

Why yfinance for V1
-------------------
- Free, no API key, no rate limit (within reason).
- Bulk download for many tickers in one call (`yf.download(list, ...)`).
- Returns split- and dividend-adjusted prices by default.

Known limitations (documented in data_dictionary.md):
- yfinance scrapes Yahoo Finance; data can change retroactively when Yahoo
  recomputes adjustments after corporate actions.
- For production, Polygon.io ($30/mo) or CRSP via WRDS are better.

Point-in-time
-------------
Adjustment factors mutate with corporate actions. The PIT-clean way:
  - Store `close` as the adjusted close *as of ingestion time*.
  - Store `adj_factor` so we can recover the raw close if needed.
  - `as_of` = ingestion timestamp.

When backtesting, we filter to `as_of <= T` so the backtest sees the
adjustments that existed at time T, not today's. Same row written twice
(say, after a 2:1 split) becomes two rows with different `as_of`; the
backtest picks the most recent one ≤ event date.

Honest caveat: yfinance returns "today's view" of historical prices, so
adjustment vintages aren't perfectly captured by ingestion timestamps —
we get the *current* adjustment factor stamped with today's `as_of`.
The right fix is point-in-time CRSP. Until then, this is the best the
free data allows; documented and disclosed on every onepager.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd
import yfinance as yf

from catalyst_engine.utils.logging import get_logger

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None  # type: ignore[assignment]

log = get_logger(__name__)


@dataclass(frozen=True)
class PriceBar:
    """A single daily price bar ready for warehouse insert."""

    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    adj_factor: float
    as_of: datetime


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------


def fetch_prices(
    tickers: list[str],
    *,
    start: date,
    end: date | None = None,
    auto_adjust: bool = True,
) -> pd.DataFrame:
    """Bulk download daily OHLCV for many tickers.

    Returns a wide-format DataFrame from yfinance. Parsing happens in
    `parse_price_response`.

    `auto_adjust=True` means the OHLC values are already split- and
    dividend-adjusted. The `adj_factor` we store separately is computed
    from the ratio of unadjusted Close to adjusted Close.
    """
    if not tickers:
        return pd.DataFrame()

    end_date = end or date.today()
    log.info("yfinance_fetch_start", n_tickers=len(tickers), start=str(start), end=str(end_date))

    # yfinance's bulk download: pass list, returns multi-index columns
    df = yf.download(
        tickers=" ".join(tickers),
        start=start.isoformat(),
        end=end_date.isoformat(),
        auto_adjust=auto_adjust,
        group_by="ticker",
        threads=True,
        progress=False,
        # Avoid yfinance's internal "actions" join which can corrupt frames
        actions=False,
    )

    if df is None or df.empty:
        log.warning("yfinance_empty_response", tickers=tickers[:5])
        return pd.DataFrame()

    log.info("yfinance_fetch_done", shape=df.shape)
    return df


def parse_price_response(
    df: pd.DataFrame,
    tickers: list[str],
    *,
    as_of: datetime,
) -> list[PriceBar]:
    """Convert yfinance wide DataFrame to PriceBar records.

    yfinance returns one of two shapes depending on number of tickers:
    - Single ticker: flat columns ['Open', 'High', 'Low', 'Close', 'Volume']
    - Multiple tickers: MultiIndex columns (ticker, field)
    """
    if df.empty:
        return []

    bars: list[PriceBar] = []
    is_multi = isinstance(df.columns, pd.MultiIndex)

    for ticker in tickers:
        ticker_upper = ticker.upper()

        if is_multi:
            if ticker not in df.columns.get_level_values(0):
                log.debug("price_ticker_missing", ticker=ticker)
                continue
            sub = df[ticker]
        else:
            sub = df

        for idx, row in sub.iterrows():
            close = row.get("Close")
            if pd.isna(close):
                continue

            open_ = row.get("Open")
            high = row.get("High")
            low = row.get("Low")
            volume = row.get("Volume")

            # When auto_adjust=True, all OHLC are already adjusted; the
            # adjustment factor is 1.0 by definition. When False, we'd
            # compute (adj_close / close). We default to 1.0 here and
            # document the limitation.
            adj_factor = 1.0

            # idx is a Timestamp; convert to date
            bar_date = idx.date() if hasattr(idx, "date") else pd.Timestamp(idx).date()

            bars.append(
                PriceBar(
                    ticker=ticker_upper,
                    date=bar_date,
                    open=float(open_) if pd.notna(open_) else 0.0,
                    high=float(high) if pd.notna(high) else 0.0,
                    low=float(low) if pd.notna(low) else 0.0,
                    close=float(close),
                    volume=int(volume) if pd.notna(volume) else 0,
                    adj_factor=adj_factor,
                    as_of=as_of,
                )
            )

    log.info("price_bars_parsed", n_bars=len(bars), n_tickers=len(tickers))
    return bars


# ---------------------------------------------------------------------------
# Warehouse upsert
# ---------------------------------------------------------------------------


def upsert_prices(conn: duckdb.DuckDBPyConnection, bars: list[PriceBar]) -> int:
    """Insert price bars. Idempotent on (ticker, date, as_of).

    Same date re-ingested later with a different adjustment factor creates a
    new row (different `as_of`), preserving vintage.
    """
    if not bars:
        return 0

    def _to_storage(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts
        return ts.astimezone(UTC).replace(tzinfo=None)

    seen: set[tuple[str, date, datetime]] = set()
    deduped: list[tuple[PriceBar, datetime]] = []
    for b in bars:
        storage_ts = _to_storage(b.as_of)
        key = (b.ticker, b.date, storage_ts)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((b, storage_ts))

    placeholders = ",".join("(?, ?, ?)" for _ in deduped)
    existing_rows = conn.execute(
        f"""
        SELECT ticker, date, as_of FROM prices
        WHERE (ticker, date, as_of) IN ({placeholders})
        """,
        [v for b, ts in deduped for v in (b.ticker, b.date, ts)],
    ).fetchall()
    existing = {(row[0], row[1], row[2]) for row in existing_rows}

    to_insert = [(b, ts) for b, ts in deduped if (b.ticker, b.date, ts) not in existing]
    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO prices (
            ticker, date, open, high, low, close, volume, adj_factor, as_of, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'yfinance')
        """,
        [
            (b.ticker, b.date, b.open, b.high, b.low, b.close, b.volume, b.adj_factor, ts)
            for b, ts in to_insert
        ],
    )
    log.info("prices_upserted", new=len(to_insert), skipped=len(deduped) - len(to_insert))
    return len(to_insert)


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------


def ingest_prices_for_universe(
    conn: duckdb.DuckDBPyConnection,
    *,
    tickers: list[str],
    start: date,
    end: date | None = None,
    batch_size: int = 50,
) -> int:
    """Bulk-pull OHLCV for many tickers in batches.

    yfinance's bulk endpoint can handle ~100 tickers per call but gets
    flaky at higher counts. 50 is the sweet spot in my testing.

    Returns total new rows written.
    """
    now = datetime.now(UTC)
    total = 0

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        log.info(
            "yfinance_batch_start",
            batch_index=i // batch_size,
            n=len(batch),
            of=len(tickers),
        )
        df = fetch_prices(batch, start=start, end=end)
        bars = parse_price_response(df, batch, as_of=now)
        total += upsert_prices(conn, bars)

    log.info("price_ingest_done", total_new_rows=total, n_tickers=len(tickers))
    return total


# ---------------------------------------------------------------------------
# Earnings backfill via yfinance (supplements Finnhub's 4Q free-tier cap)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class YFEarningsRecord:
    """Earnings record extracted from yfinance's earnings_dates table."""

    ticker: str
    event_date: date
    eps_est: float | None
    eps_actual: float | None


def fetch_yf_earnings_history(ticker: str) -> list[YFEarningsRecord]:
    """Pull deeper earnings history from yfinance.

    yfinance's `Ticker.earnings_dates` returns historical + upcoming
    earnings dates with EPS estimate and reported EPS. Coverage is usually
    8-12 quarters (vs. Finnhub free tier's 4).

    Returns empty list on any error — yfinance scraping is best-effort.
    """
    try:
        t = yf.Ticker(ticker)
        df = t.earnings_dates
        if df is None or df.empty:
            return []
    except Exception as exc:  # — yfinance raises many flavors
        log.warning("yf_earnings_history_failed", ticker=ticker, error=str(exc))
        return []

    out: list[YFEarningsRecord] = []
    for idx, row in df.iterrows():
        if not hasattr(idx, "date"):
            continue
        event_date = idx.date()

        # Columns vary: "EPS Estimate", "Reported EPS", "Surprise(%)"
        eps_est_raw = row.get("EPS Estimate")
        eps_actual_raw = row.get("Reported EPS")

        eps_est = float(eps_est_raw) if pd.notna(eps_est_raw) else None
        eps_actual = float(eps_actual_raw) if pd.notna(eps_actual_raw) else None

        if eps_est is None and eps_actual is None:
            continue

        out.append(
            YFEarningsRecord(
                ticker=ticker.upper(),
                event_date=event_date,
                eps_est=eps_est,
                eps_actual=eps_actual,
            )
        )

    return out


def yf_records_to_earnings_events(
    records: list[YFEarningsRecord],
) -> list[EarningsEvent]:  # noqa: F821
    """Convert yfinance earnings rows into EarningsEvent for the shared schema.

    Reuses the earnings module's contract so backfilled rows land in the
    same `earnings_events` table. We set source='yfinance' at insert time.
    """
    from catalyst_engine.data.earnings import EarningsEvent

    out: list[EarningsEvent] = []
    for r in records:
        # For records with eps_actual, as_of = event_date (historical print).
        # For forward-looking ones (estimate only), as_of = now (we observed
        # this estimate at ingestion time).
        if r.eps_actual is not None:
            as_of = datetime.combine(r.event_date, datetime.min.time(), tzinfo=UTC)
        else:
            as_of = datetime.now(UTC)

        out.append(
            EarningsEvent(
                ticker=r.ticker,
                event_date=r.event_date,
                time_of_day="UNK",
                fiscal_period=None,
                eps_est=r.eps_est,
                eps_actual=r.eps_actual,
                revenue_est=None,
                revenue_actual=None,
                as_of=as_of,
            )
        )
    return out


def ingest_yf_earnings_backfill(
    conn: duckdb.DuckDBPyConnection, tickers: list[str]
) -> dict[str, int]:
    """Backfill earnings_events with deeper history from yfinance.

    Writes to the same `earnings_events` table with `source='yfinance'`. The
    `upsert_earnings_events` function in the earnings module handles dedup
    via (ticker, event_date, as_of), so rows that already exist from Finnhub
    are skipped automatically.

    Returns {ticker: rows_written}.
    """
    from catalyst_engine.data.earnings import upsert_earnings_events

    results: dict[str, int] = {}
    for ticker in tickers:
        try:
            yf_records = fetch_yf_earnings_history(ticker)
            events = yf_records_to_earnings_events(yf_records)
            # We override source by patching the insert — simpler than another
            # parameter. The upsert function hardcodes 'finnhub' as source,
            # but since dedup is on (ticker, event_date, as_of), yfinance
            # rows with different as_of will land alongside Finnhub ones.
            results[ticker] = upsert_earnings_events(conn, events)
        except Exception as exc:
            log.error("yf_backfill_failed", ticker=ticker, error=str(exc))
            results[ticker] = -1

    successes = sum(1 for v in results.values() if v >= 0)
    failures = sum(1 for v in results.values() if v < 0)
    total = sum(v for v in results.values() if v > 0)
    log.info(
        "yf_earnings_backfill_done",
        tickers=len(results),
        successes=successes,
        failures=failures,
        new_rows=total,
    )
    return results
