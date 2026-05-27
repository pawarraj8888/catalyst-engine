"""Tests for prices.py — yfinance OHLCV + earnings backfill."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import pandas as pd

from catalyst_engine.data.prices import (
    PriceBar,
    YFEarningsRecord,
    parse_price_response,
    upsert_prices,
    yf_records_to_earnings_events,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _multi_index_fixture() -> pd.DataFrame:
    """yfinance multi-ticker shape: MultiIndex columns (ticker, field)."""
    idx = pd.DatetimeIndex(["2025-08-01", "2025-08-04", "2025-08-05"], name="Date")
    cols = pd.MultiIndex.from_product(
        [["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    data = [
        [220.0, 222.0, 219.0, 221.5, 50_000_000, 410.0, 412.0, 408.0, 411.0, 22_000_000],
        [222.0, 224.0, 221.0, 223.0, 48_000_000, 411.0, 414.0, 410.0, 413.0, 21_000_000],
        [223.0, 225.0, 222.0, 224.5, 55_000_000, 413.0, 416.0, 412.0, 415.0, 23_000_000],
    ]
    return pd.DataFrame(data, index=idx, columns=cols)


def _flat_fixture() -> pd.DataFrame:
    """yfinance single-ticker shape: flat columns."""
    idx = pd.DatetimeIndex(["2025-08-01", "2025-08-04"], name="Date")
    return pd.DataFrame(
        {
            "Open": [220.0, 222.0],
            "High": [222.0, 224.0],
            "Low": [219.0, 221.0],
            "Close": [221.5, 223.0],
            "Volume": [50_000_000, 48_000_000],
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# parse_price_response
# ---------------------------------------------------------------------------


def test_parse_multi_ticker_shape() -> None:
    df = _multi_index_fixture()
    as_of = datetime(2025, 8, 6, tzinfo=UTC)
    bars = parse_price_response(df, ["AAPL", "MSFT"], as_of=as_of)
    # 3 dates x 2 tickers
    assert len(bars) == 6
    assert {b.ticker for b in bars} == {"AAPL", "MSFT"}


def test_parse_single_ticker_flat_shape() -> None:
    df = _flat_fixture()
    as_of = datetime(2025, 8, 6, tzinfo=UTC)
    bars = parse_price_response(df, ["AAPL"], as_of=as_of)
    assert len(bars) == 2
    assert all(b.ticker == "AAPL" for b in bars)
    assert bars[0].date == date(2025, 8, 1)
    assert bars[0].close == 221.5
    assert bars[0].volume == 50_000_000


def test_parse_skips_missing_ticker() -> None:
    df = _multi_index_fixture()
    as_of = datetime(2025, 8, 6, tzinfo=UTC)
    bars = parse_price_response(df, ["AAPL", "NOTREAL"], as_of=as_of)
    assert {b.ticker for b in bars} == {"AAPL"}


def test_parse_empty_df() -> None:
    as_of = datetime(2025, 8, 6, tzinfo=UTC)
    assert parse_price_response(pd.DataFrame(), ["AAPL"], as_of=as_of) == []


def test_parse_handles_nan_close() -> None:
    """Days where Close is NaN (e.g. halted stock) are dropped."""
    idx = pd.DatetimeIndex(["2025-08-01", "2025-08-02"], name="Date")
    df = pd.DataFrame(
        {
            "Open": [220.0, 221.0],
            "High": [222.0, 223.0],
            "Low": [219.0, 220.0],
            "Close": [221.5, float("nan")],  # NaN close
            "Volume": [50_000_000, 0],
        },
        index=idx,
    )
    as_of = datetime(2025, 8, 6, tzinfo=UTC)
    bars = parse_price_response(df, ["AAPL"], as_of=as_of)
    assert len(bars) == 1
    assert bars[0].date == date(2025, 8, 1)


def test_parse_uppercases_tickers() -> None:
    df = _flat_fixture()
    as_of = datetime(2025, 8, 6, tzinfo=UTC)
    bars = parse_price_response(df, ["aapl"], as_of=as_of)
    assert all(b.ticker == "AAPL" for b in bars)


# ---------------------------------------------------------------------------
# upsert_prices
# ---------------------------------------------------------------------------


def _bar(ticker: str = "AAPL", d: date = date(2025, 8, 1), close: float = 221.5) -> PriceBar:
    return PriceBar(
        ticker=ticker,
        date=d,
        open=220.0,
        high=222.0,
        low=219.0,
        close=close,
        volume=50_000_000,
        adj_factor=1.0,
        as_of=datetime(2025, 8, 1, 20, 0, tzinfo=UTC),
    )


def test_upsert_writes_bars(warehouse: duckdb.DuckDBPyConnection) -> None:
    n = upsert_prices(warehouse, [_bar()])
    assert n == 1
    count = warehouse.execute("SELECT COUNT(*) FROM prices").fetchone()
    assert count is not None and count[0] == 1


def test_upsert_idempotent(warehouse: duckdb.DuckDBPyConnection) -> None:
    bar = _bar()
    assert upsert_prices(warehouse, [bar]) == 1
    assert upsert_prices(warehouse, [bar]) == 0


def test_upsert_dedupes_within_batch(warehouse: duckdb.DuckDBPyConnection) -> None:
    bar = _bar()
    assert upsert_prices(warehouse, [bar, bar, bar]) == 1


def test_upsert_different_dates_writes_all(warehouse: duckdb.DuckDBPyConnection) -> None:
    bars = [
        _bar(d=date(2025, 8, 1)),
        _bar(d=date(2025, 8, 4)),
        _bar(d=date(2025, 8, 5)),
    ]
    assert upsert_prices(warehouse, bars) == 3


def test_upsert_empty_is_noop(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert upsert_prices(warehouse, []) == 0


# ---------------------------------------------------------------------------
# yfinance earnings backfill
# ---------------------------------------------------------------------------


def test_yf_records_to_earnings_events_actuals_use_event_date_as_of() -> None:
    rec = YFEarningsRecord(
        ticker="AAPL", event_date=date(2025, 8, 1), eps_est=1.35, eps_actual=1.41
    )
    events = yf_records_to_earnings_events([rec])
    assert len(events) == 1
    e = events[0]
    assert e.ticker == "AAPL"
    assert e.eps_actual == 1.41
    assert e.as_of == datetime(2025, 8, 1, tzinfo=UTC)


def test_yf_records_estimate_only_uses_ingestion_time_as_of() -> None:
    """Forward-looking estimate → as_of = ingestion time (close to now)."""
    rec = YFEarningsRecord(ticker="AAPL", event_date=date(2099, 1, 1), eps_est=2.0, eps_actual=None)
    events = yf_records_to_earnings_events([rec])
    # as_of should be close to now, not the future event date
    now = datetime.now(UTC)
    delta = abs((events[0].as_of - now).total_seconds())
    assert delta < 10  # generous fudge for slow tests


def test_yf_records_empty_input() -> None:
    assert yf_records_to_earnings_events([]) == []
