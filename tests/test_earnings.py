"""Tests for Finnhub earnings ingestion.

Network calls are not made here. The @pytest.mark.integration test that
hits Finnhub live is skipped by default.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import pytest

from catalyst_engine.data.earnings import (
    EarningsEvent,
    _coerce_float,
    _parse_time_of_day,
    parse_calendar_response,
    parse_surprise_response,
    upsert_earnings_events,
)

# ---------------------------------------------------------------------------
# Fixture payloads modeled on real Finnhub responses
# ---------------------------------------------------------------------------

CALENDAR_FIXTURE = [
    {
        "symbol": "AAPL",
        "date": "2025-08-01",
        "hour": "amc",
        "quarter": 3,
        "year": 2025,
        "epsEstimate": 1.35,
        "epsActual": 1.41,
        "revenueEstimate": 84200000000,
        "revenueActual": 85800000000,
    },
    {
        "symbol": "MSFT",
        "date": "2026-07-30",
        "hour": "amc",
        "quarter": 4,
        "year": 2026,
        "epsEstimate": 3.10,
        "epsActual": None,
        "revenueEstimate": 65000000000,
        "revenueActual": None,
    },
    {
        # Outside the universe — should be filtered when universe_tickers is set
        "symbol": "OUTOFUNIVERSE",
        "date": "2026-08-15",
        "hour": "bmo",
        "epsEstimate": 0.5,
    },
    {
        # Malformed / empty — should be skipped without raising
        "symbol": "",
        "date": "2026-08-15",
    },
    {
        # Bad date — should be skipped
        "symbol": "BAD",
        "date": "not-a-date",
    },
]

SURPRISE_FIXTURE = [
    {"period": "2025-08-01", "actual": 1.41, "estimate": 1.35, "quarter": 3, "year": 2025},
    {"period": "2025-05-02", "actual": 1.65, "estimate": 1.52, "quarter": 2, "year": 2025},
    {"period": "2025-02-01", "actual": 2.40, "estimate": 2.36, "quarter": 1, "year": 2025},
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_parse_time_of_day_known_values() -> None:
    assert _parse_time_of_day("bmo") == "BMO"
    assert _parse_time_of_day("amc") == "AMC"
    assert _parse_time_of_day("BMO") == "BMO"
    assert _parse_time_of_day("dmh") == "DMH"


def test_parse_time_of_day_unknown_defaults_to_unk() -> None:
    assert _parse_time_of_day(None) == "UNK"
    assert _parse_time_of_day("") == "UNK"
    assert _parse_time_of_day("garbage") == "UNK"


def test_coerce_float_handles_messy_inputs() -> None:
    assert _coerce_float(1.5) == 1.5
    assert _coerce_float("1.5") == 1.5
    assert _coerce_float(0) == 0.0
    assert _coerce_float(None) is None
    assert _coerce_float("") is None
    assert _coerce_float("not a number") is None


# ---------------------------------------------------------------------------
# Calendar parsing
# ---------------------------------------------------------------------------


def test_parse_calendar_basic_shape() -> None:
    ingest_time = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    records = parse_calendar_response(CALENDAR_FIXTURE, as_of=ingest_time)
    # AAPL + MSFT + OUTOFUNIVERSE survive; "" symbol and bad date are dropped
    tickers = [r.ticker for r in records]
    assert tickers == ["AAPL", "MSFT", "OUTOFUNIVERSE"]


def test_parse_calendar_filters_universe() -> None:
    ingest_time = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    records = parse_calendar_response(
        CALENDAR_FIXTURE, as_of=ingest_time, universe_tickers={"AAPL", "MSFT"}
    )
    assert {r.ticker for r in records} == {"AAPL", "MSFT"}


def test_parse_calendar_as_of_semantics() -> None:
    """as_of for historical rows = event_date; for upcoming = ingestion time.

    This is the PIT contract spelled out in the module docstring.
    """
    ingest_time = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    records = parse_calendar_response(
        CALENDAR_FIXTURE, as_of=ingest_time, universe_tickers={"AAPL", "MSFT"}
    )
    aapl = next(r for r in records if r.ticker == "AAPL")
    msft = next(r for r in records if r.ticker == "MSFT")

    # AAPL has eps_actual → historical → as_of pinned to event_date
    assert aapl.eps_actual is not None
    assert aapl.as_of == datetime(2025, 8, 1, tzinfo=UTC)

    # MSFT has no actual → forward-looking → as_of = ingestion time
    assert msft.eps_actual is None
    assert msft.as_of == ingest_time


def test_parse_calendar_extracts_fields() -> None:
    ingest_time = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    records = parse_calendar_response(
        CALENDAR_FIXTURE, as_of=ingest_time, universe_tickers={"AAPL"}
    )
    aapl = records[0]
    assert aapl.event_date == date(2025, 8, 1)
    assert aapl.time_of_day == "AMC"
    assert aapl.fiscal_period == "Q3 2025"
    assert aapl.eps_est == 1.35
    assert aapl.eps_actual == 1.41
    assert aapl.revenue_est == 84200000000
    assert aapl.revenue_actual == 85800000000


def test_parse_calendar_handles_empty_input() -> None:
    ingest_time = datetime(2026, 5, 27, tzinfo=UTC)
    assert parse_calendar_response([], as_of=ingest_time) == []


# ---------------------------------------------------------------------------
# Surprise parsing
# ---------------------------------------------------------------------------


def test_parse_surprises_returns_one_record_per_period() -> None:
    records = parse_surprise_response(SURPRISE_FIXTURE, ticker="AAPL")
    assert len(records) == 3
    assert all(r.ticker == "AAPL" for r in records)


def test_parse_surprises_as_of_equals_event_date() -> None:
    records = parse_surprise_response(SURPRISE_FIXTURE, ticker="AAPL")
    for r in records:
        expected = datetime.combine(r.event_date, datetime.min.time(), tzinfo=UTC)
        assert r.as_of == expected


def test_parse_surprises_skips_bad_periods() -> None:
    bad = [
        {"period": "2025-08-01", "actual": 1.41, "estimate": 1.35},
        {"period": "not-a-date", "actual": 1.0, "estimate": 1.0},
        {"period": None, "actual": 1.0},
        {},
    ]
    records = parse_surprise_response(bad, ticker="AAPL")
    assert len(records) == 1
    assert records[0].event_date == date(2025, 8, 1)


def test_parse_surprises_uppercases_ticker() -> None:
    records = parse_surprise_response(SURPRISE_FIXTURE, ticker="aapl")
    assert all(r.ticker == "AAPL" for r in records)


# ---------------------------------------------------------------------------
# Warehouse upsert
# ---------------------------------------------------------------------------


def _make_event(
    *,
    ticker: str = "AAPL",
    event_date: date = date(2025, 8, 1),
    as_of: datetime | None = None,
    eps_est: float | None = 1.35,
    eps_actual: float | None = 1.41,
) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker,
        event_date=event_date,
        time_of_day="AMC",
        fiscal_period="Q3 2025",
        eps_est=eps_est,
        eps_actual=eps_actual,
        revenue_est=None,
        revenue_actual=None,
        as_of=as_of or datetime(2025, 8, 1, tzinfo=UTC),
    )


def test_upsert_writes_records(warehouse: duckdb.DuckDBPyConnection) -> None:
    n = upsert_earnings_events(warehouse, [_make_event()])
    assert n == 1
    row = warehouse.execute("SELECT COUNT(*) FROM earnings_events").fetchone()
    assert row is not None and row[0] == 1


def test_upsert_is_idempotent_on_same_key(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Same (ticker, event_date, as_of) twice — only one row lands."""
    event = _make_event()
    assert upsert_earnings_events(warehouse, [event]) == 1
    assert upsert_earnings_events(warehouse, [event]) == 0

    count = warehouse.execute("SELECT COUNT(*) FROM earnings_events").fetchone()
    assert count is not None and count[0] == 1


def test_upsert_preserves_estimate_revisions(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Same event with a different as_of writes a NEW row.

    This is the contract for tracking estimate revisions: each new observation
    is a new row, never overwrite.
    """
    from decimal import Decimal

    e1 = _make_event(as_of=datetime(2025, 7, 1, tzinfo=UTC), eps_est=1.30, eps_actual=None)
    e2 = _make_event(as_of=datetime(2025, 7, 15, tzinfo=UTC), eps_est=1.33, eps_actual=None)
    e3 = _make_event(as_of=datetime(2025, 7, 28, tzinfo=UTC), eps_est=1.35, eps_actual=None)
    assert upsert_earnings_events(warehouse, [e1, e2, e3]) == 3

    rows = warehouse.execute(
        "SELECT as_of, eps_est FROM earnings_events "
        "WHERE ticker = 'AAPL' AND event_date = '2025-08-01' "
        "ORDER BY as_of"
    ).fetchall()
    # eps_est is stored as DECIMAL(18, 4) so DuckDB returns Decimal, not float.
    # That precision is intentional — money values should not be float-compared.
    assert [float(r[1]) for r in rows] == [1.30, 1.33, 1.35]
    # Same for the underlying Decimals — at least verify quantization is exact:
    assert [r[1] for r in rows] == [
        Decimal("1.3000"),
        Decimal("1.3300"),
        Decimal("1.3500"),
    ]


def test_upsert_dedupes_within_batch(warehouse: duckdb.DuckDBPyConnection) -> None:
    e = _make_event()
    # Same record three times in one batch
    n = upsert_earnings_events(warehouse, [e, e, e])
    assert n == 1


def test_upsert_empty_list_is_noop(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert upsert_earnings_events(warehouse, []) == 0


# ---------------------------------------------------------------------------
# Live integration (skipped in CI)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_finnhub_calendar() -> None:
    """Live call to Finnhub. Skipped unless integration tests are enabled.

    Run with: pytest -m integration
    Requires FINNHUB_API_KEY in env.
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    from catalyst_engine.data.earnings import (
        FinnhubRateLimiter,
        _build_client,
        fetch_calendar,
    )

    today = _date.today()
    with _build_client() as client:
        entries = fetch_calendar(
            client, FinnhubRateLimiter(), start=today, end=today + _td(days=14)
        )
        assert isinstance(entries, list)


# ---------------------------------------------------------------------------
# Orchestration tests (mock httpx; no network)
# ---------------------------------------------------------------------------


def test_ingest_calendar_window_chunks_and_inserts(
    warehouse: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ingest_calendar_window should call the API across multiple chunks
    and persist all parsed records to the warehouse."""
    from datetime import date as _date

    from catalyst_engine.data import earnings as earnings_module

    # Track which chunks were requested
    calls: list[tuple[str, str]] = []

    def fake_get_json(client, path, params, limiter):  # type: ignore[no-untyped-def]
        calls.append((params["from"], params["to"]))
        return {
            "earningsCalendar": [
                {
                    "symbol": "AAPL",
                    "date": params["from"],  # one event per chunk for simplicity
                    "hour": "amc",
                    "epsEstimate": 1.0,
                    "epsActual": None,
                    "quarter": 3,
                    "year": 2026,
                }
            ]
        }

    # Patch the API client + JSON fetcher
    monkeypatch.setattr(earnings_module, "_get_json", fake_get_json)
    monkeypatch.setattr(earnings_module, "_build_client", lambda: _NoOpClient())

    n = earnings_module.ingest_calendar_window(
        warehouse,
        start=_date(2026, 6, 1),
        end=_date(2026, 8, 30),  # 91 days → 4 chunks at chunk_days=30
        chunk_days=30,
    )

    # 4 API calls (4 chunks) but some may dedupe; we expect at least 1 write
    assert len(calls) == 4
    assert n >= 1
    # Confirm warehouse has the rows
    count = warehouse.execute("SELECT COUNT(*) FROM earnings_events").fetchone()
    assert count is not None and count[0] >= 1


def test_ingest_surprise_history_handles_errors(
    warehouse: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticker that errors should land as -1 in results, not crash the run."""
    import httpx

    from catalyst_engine.data import earnings as earnings_module

    def fake_get_json(client, path, params, limiter):  # type: ignore[no-untyped-def]
        if params["symbol"] == "BAD":
            raise httpx.HTTPError("boom")
        return [
            {
                "period": "2025-08-01",
                "actual": 1.41,
                "estimate": 1.35,
                "quarter": 3,
                "year": 2025,
            }
        ]

    monkeypatch.setattr(earnings_module, "_get_json", fake_get_json)
    monkeypatch.setattr(earnings_module, "_build_client", lambda: _NoOpClient())

    results = earnings_module.ingest_surprise_history(warehouse, tickers=["AAPL", "BAD", "MSFT"])
    assert results["AAPL"] == 1
    assert results["BAD"] == -1
    assert results["MSFT"] == 1


def test_finnhub_rate_limiter_smoke() -> None:
    """Limiter shouldn't crash; first call shouldn't sleep."""
    import time as _time

    from catalyst_engine.data.earnings import FinnhubRateLimiter

    limiter = FinnhubRateLimiter(calls_per_minute=600)  # fast
    t0 = _time.monotonic()
    limiter.wait()  # first call
    limiter.wait()  # second call should sleep ~0.1s
    elapsed = _time.monotonic() - t0
    # Should be at least the minimum interval between calls
    assert elapsed >= 0.05


class _NoOpClient:
    """Minimal stand-in for httpx.Client in mocked tests."""

    def __enter__(self) -> _NoOpClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise RuntimeError("should be patched in tests")
