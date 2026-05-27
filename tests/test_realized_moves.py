"""Tests for the realized_moves feature module."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import pytest

from catalyst_engine.features.realized_moves import (
    RealizedMove,
    attach_trailing_medians,
    compute_event_moves,
    compute_universe_moves,
    previous_trading_close,
    trading_close_at_offset,
    upsert_realized_moves,
)

UTC = UTC


# ---------------------------------------------------------------------------
# Helpers to load fixture price/earnings data
# ---------------------------------------------------------------------------


def _insert_prices(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    bars: list[tuple[date, float]],
    *,
    as_of: datetime | None = None,
) -> None:
    as_of_ts = (as_of or datetime(2026, 1, 1, tzinfo=UTC)).replace(tzinfo=None)
    conn.executemany(
        """
        INSERT INTO prices (ticker, date, open, high, low, close, volume, adj_factor, as_of, source)
        VALUES (?, ?, ?, ?, ?, ?, 1000000, 1.0, ?, 'test')
        """,
        [(ticker, d, c, c, c, c, as_of_ts) for d, c in bars],
    )


def _insert_earnings(
    conn: duckdb.DuckDBPyConnection,
    rows: list[tuple[str, date, float]],
) -> None:
    """Insert earnings events with eps_actual set so the universe-compute pass picks them up."""
    conn.executemany(
        """
        INSERT INTO earnings_events
        (ticker, event_date, time_of_day, fiscal_period, eps_est, eps_actual,
         revenue_est, revenue_actual, as_of, source)
        VALUES (?, ?, 'UNK', NULL, NULL, ?, NULL, NULL, ?, 'test')
        """,
        [(t, d, actual, datetime(d.year, d.month, d.day)) for t, d, actual in rows],
    )


# ---------------------------------------------------------------------------
# Price lookup primitives
# ---------------------------------------------------------------------------


def test_previous_trading_close_returns_strictly_prior(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    _insert_prices(
        warehouse,
        "AAPL",
        [
            (date(2025, 7, 30), 100.0),
            (date(2025, 7, 31), 101.0),
            (date(2025, 8, 1), 105.0),  # the earnings day itself
        ],
    )
    # Event on Aug 1 — previous close must be Jul 31, NOT Aug 1
    result = previous_trading_close(warehouse, "AAPL", date(2025, 8, 1))
    assert result == (date(2025, 7, 31), 101.0)


def test_previous_trading_close_handles_weekend_gap(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Monday earnings — previous close is Friday."""
    _insert_prices(
        warehouse,
        "AAPL",
        [
            (date(2025, 8, 1), 100.0),  # Friday
            # (no Sat/Sun bars)
            (date(2025, 8, 4), 103.0),  # Monday earnings day
        ],
    )
    result = previous_trading_close(warehouse, "AAPL", date(2025, 8, 4))
    assert result == (date(2025, 8, 1), 100.0)


def test_previous_trading_close_returns_none_for_ipo(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """No prior history → None, not an exception."""
    _insert_prices(warehouse, "IPO", [(date(2025, 8, 1), 50.0)])
    assert previous_trading_close(warehouse, "IPO", date(2025, 8, 1)) is None


def test_trading_close_at_offset_skips_weekends(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Friday earnings, T+1 should resolve to Friday (the event day's close),
    T+5 should skip the weekend correctly."""
    _insert_prices(
        warehouse,
        "AAPL",
        [
            (date(2025, 8, 1), 100.0),  # Fri — event day
            (date(2025, 8, 4), 101.0),  # Mon
            (date(2025, 8, 5), 102.0),
            (date(2025, 8, 6), 103.0),
            (date(2025, 8, 7), 104.0),
            (date(2025, 8, 8), 105.0),  # 5th trading day from Aug 1
        ],
    )
    # 1st trading day at or after Aug 1 is Aug 1 itself
    r1 = trading_close_at_offset(warehouse, "AAPL", date(2025, 8, 1), n_trading_days_after=1)
    assert r1 == (date(2025, 8, 1), 100.0)
    # 5th is Aug 7 (Aug 1, 4, 5, 6, 7)
    r5 = trading_close_at_offset(warehouse, "AAPL", date(2025, 8, 1), n_trading_days_after=5)
    assert r5 == (date(2025, 8, 7), 104.0)


def test_trading_close_at_offset_returns_none_when_insufficient(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    _insert_prices(warehouse, "AAPL", [(date(2025, 8, 1), 100.0)])
    # Asking for the 5th trading day, only 1 exists
    assert (
        trading_close_at_offset(warehouse, "AAPL", date(2025, 8, 1), n_trading_days_after=5) is None
    )


def test_trading_close_at_offset_rejects_zero(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(ValueError):
        trading_close_at_offset(warehouse, "AAPL", date(2025, 8, 1), n_trading_days_after=0)


# ---------------------------------------------------------------------------
# compute_event_moves
# ---------------------------------------------------------------------------


def test_compute_event_moves_basic(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Beat scenario: pre=100, post=110 → +10% 1-day move."""
    _insert_prices(
        warehouse,
        "AAPL",
        [
            (date(2025, 7, 31), 100.0),
            (date(2025, 8, 1), 110.0),
            (date(2025, 8, 4), 108.0),
            (date(2025, 8, 5), 107.0),
            (date(2025, 8, 6), 109.0),
            (date(2025, 8, 7), 112.0),
        ],
    )
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    m = compute_event_moves(warehouse, "AAPL", date(2025, 8, 1), as_of=as_of)

    assert m.pre_close == 100.0
    assert m.post_close_1d == 110.0
    assert m.realized_move_1d == pytest.approx(0.10)
    assert m.abs_move_1d == pytest.approx(0.10)
    # 5th trading day is Aug 7
    assert m.post_close_5d == 112.0
    assert m.realized_move_5d == pytest.approx(0.12)


def test_compute_event_moves_downside(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Miss scenario: signed move is negative, abs is positive."""
    _insert_prices(
        warehouse,
        "MSFT",
        [
            (date(2025, 7, 31), 400.0),
            (date(2025, 8, 1), 380.0),
        ],
    )
    m = compute_event_moves(
        warehouse, "MSFT", date(2025, 8, 1), as_of=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert m.realized_move_1d == pytest.approx(-0.05)
    assert m.abs_move_1d == pytest.approx(0.05)


def test_compute_event_moves_handles_missing_history(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Event for ticker with no prior price → fields are None, not exception."""
    m = compute_event_moves(
        warehouse, "GHOST", date(2025, 8, 1), as_of=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert m.pre_close is None
    assert m.realized_move_1d is None
    assert m.abs_move_1d is None


# ---------------------------------------------------------------------------
# Trailing median — PIT strictness
# ---------------------------------------------------------------------------


def _move(ticker: str, ed: date, abs_1d: float) -> RealizedMove:
    """Helper to build a RealizedMove with just enough fields for median tests."""
    return RealizedMove(
        ticker=ticker,
        event_date=ed,
        pre_close_date=None,
        post_close_date_1d=None,
        post_close_date_5d=None,
        pre_close=None,
        post_close_1d=None,
        post_close_5d=None,
        realized_move_1d=abs_1d,
        realized_move_5d=None,
        abs_move_1d=abs_1d,
        trailing_median_8q=None,
        n_prior_events=0,
        move_ratio=None,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_trailing_median_excludes_self_and_future() -> None:
    """The window for event N must contain ONLY events strictly before N.

    This is the single most important PIT test in this module. If this
    fails the backtest leaks future information.
    """
    moves = [
        _move("AAPL", date(2024, 1, 1), 0.02),
        _move("AAPL", date(2024, 4, 1), 0.04),
        _move("AAPL", date(2024, 7, 1), 0.06),
        _move("AAPL", date(2024, 10, 1), 0.08),
    ]
    out = attach_trailing_medians(moves, window=8)

    # First event has no prior history — median is None
    assert out[0].trailing_median_8q is None
    assert out[0].n_prior_events == 0

    # Second event: only the first event is in scope (0.02)
    assert out[1].trailing_median_8q == pytest.approx(0.02)
    assert out[1].n_prior_events == 1

    # Third event: prior = [0.02, 0.04] → median 0.03
    assert out[2].trailing_median_8q == pytest.approx(0.03)
    assert out[2].n_prior_events == 2

    # Fourth event: prior = [0.02, 0.04, 0.06] → median 0.04
    assert out[3].trailing_median_8q == pytest.approx(0.04)
    assert out[3].n_prior_events == 3


def test_trailing_median_window_cap() -> None:
    """Window=2 means we only keep the most recent 2 prior values."""
    moves = [_move("X", date(2024, i + 1, 1), 0.1 * (i + 1)) for i in range(5)]
    out = attach_trailing_medians(moves, window=2)
    # 5th event's prior window = last 2 of [0.1, 0.2, 0.3, 0.4] = [0.3, 0.4] → median 0.35
    assert out[4].trailing_median_8q == pytest.approx(0.35)
    assert out[4].n_prior_events == 2


def test_trailing_median_input_order_does_not_matter() -> None:
    """Even if events arrive unsorted, the function sorts by event_date."""
    ordered_dates = [date(2024, i + 1, 1) for i in range(4)]
    unordered = [_move("X", ordered_dates[i], 0.1 * (i + 1)) for i in [2, 0, 3, 1]]
    out_unordered = attach_trailing_medians(unordered, window=8)

    ordered = [_move("X", ordered_dates[i], 0.1 * (i + 1)) for i in range(4)]
    out_ordered = attach_trailing_medians(ordered, window=8)

    # The outputs (sorted by date) must agree on the median value at each step
    by_date_unord = {m.event_date: m.trailing_median_8q for m in out_unordered}
    by_date_ord = {m.event_date: m.trailing_median_8q for m in out_ordered}
    assert by_date_unord == by_date_ord


def test_trailing_median_per_ticker_isolation() -> None:
    """Two tickers' events must not contaminate each other's windows."""
    moves = [
        _move("AAA", date(2024, 1, 1), 0.10),
        _move("BBB", date(2024, 2, 1), 0.50),  # huge BBB move
        _move("AAA", date(2024, 3, 1), 0.20),
    ]
    out = attach_trailing_medians(moves, window=8)
    by = {(m.ticker, m.event_date): m for m in out}
    # AAA's second event should ONLY see AAA's first
    aaa2 = by[("AAA", date(2024, 3, 1))]
    assert aaa2.trailing_median_8q == pytest.approx(0.10)
    assert aaa2.n_prior_events == 1


def test_trailing_median_skips_none_abs_moves() -> None:
    """Events with abs_move_1d=None contribute no rolling history but still
    appear in output (with median=None)."""
    moves = [
        _move("X", date(2024, 1, 1), 0.10),
        RealizedMove(  # missing abs_move_1d
            ticker="X",
            event_date=date(2024, 2, 1),
            pre_close_date=None,
            post_close_date_1d=None,
            post_close_date_5d=None,
            pre_close=None,
            post_close_1d=None,
            post_close_5d=None,
            realized_move_1d=None,
            realized_move_5d=None,
            abs_move_1d=None,
            trailing_median_8q=None,
            n_prior_events=0,
            move_ratio=None,
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        _move("X", date(2024, 3, 1), 0.20),
    ]
    out = attach_trailing_medians(moves, window=8)
    # 3rd event sees only the 1st (the 2nd had no abs_move)
    third = next(m for m in out if m.event_date == date(2024, 3, 1))
    assert third.trailing_median_8q == pytest.approx(0.10)
    assert third.n_prior_events == 1


def test_move_ratio_computed() -> None:
    """move_ratio = abs_move_1d / trailing_median_8q."""
    moves = [
        _move("X", date(2024, 1, 1), 0.02),
        _move("X", date(2024, 4, 1), 0.04),
        _move("X", date(2024, 7, 1), 0.10),  # big print
    ]
    out = attach_trailing_medians(moves, window=8)
    third = out[2]
    # prior window = [0.02, 0.04] → median 0.03 → ratio = 0.10 / 0.03 ≈ 3.33
    assert third.move_ratio == pytest.approx(0.10 / 0.03, rel=1e-3)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def test_upsert_writes_rows(warehouse: duckdb.DuckDBPyConnection) -> None:
    moves = [_move("AAPL", date(2024, 1, 1), 0.05)]
    n = upsert_realized_moves(warehouse, moves)
    assert n == 1
    count = warehouse.execute("SELECT COUNT(*) FROM realized_moves").fetchone()
    assert count is not None and count[0] == 1


def test_upsert_is_idempotent(warehouse: duckdb.DuckDBPyConnection) -> None:
    moves = [_move("AAPL", date(2024, 1, 1), 0.05)]
    assert upsert_realized_moves(warehouse, moves) == 1
    assert upsert_realized_moves(warehouse, moves) == 0


def test_upsert_empty_is_noop(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert upsert_realized_moves(warehouse, []) == 0


# ---------------------------------------------------------------------------
# End-to-end on a fixture warehouse
# ---------------------------------------------------------------------------


def test_compute_universe_moves_end_to_end(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Wire-it-all-together test: 3 earnings for one ticker, 5y of prices,
    verify the realized_moves table fills with sensible numbers."""
    # 3 quarterly earnings dates
    earnings_dates = [date(2024, 1, 30), date(2024, 4, 30), date(2024, 7, 31)]
    _insert_earnings(warehouse, [("AAPL", d, 1.5) for d in earnings_dates])

    # Surrounding prices: 5 days before/after each event with simple +/- moves
    prices: list[tuple[date, float]] = []
    base_price = 100.0
    for ed in earnings_dates:
        # 10 trading days before through 10 after — synthetic continuous range
        # (We're not modeling weekends — DuckDB doesn't care about Saturdays
        # for this test as long as dates are strictly ordered.)
        for offset in range(-10, 11):
            d_offset = ed.toordinal() + offset
            prices.append((date.fromordinal(d_offset), base_price + offset * 0.5))
        base_price += 5.0  # drift between events
    # Dedupe by date keeping latest price
    by_date: dict[date, float] = {}
    for d, c in prices:
        by_date[d] = c
    _insert_prices(warehouse, "AAPL", sorted(by_date.items()))

    n = compute_universe_moves(warehouse, only_with_actuals=True, window=8)
    assert n == 3

    rows = warehouse.execute(
        "SELECT event_date, realized_move_1d, n_prior_events, trailing_median_8q "
        "FROM realized_moves ORDER BY event_date"
    ).fetchall()
    assert len(rows) == 3
    # 1st event has no prior history
    assert rows[0][2] == 0
    assert rows[0][3] is None
    # 2nd event has 1 prior, median populated
    assert rows[1][2] == 1
    assert rows[1][3] is not None
    # 3rd event has 2 priors
    assert rows[2][2] == 2
