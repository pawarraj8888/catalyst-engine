"""Realized earnings moves — the backtest label set.

For each historical earnings event, compute:

- ``realized_move_1d``  - the 1-day move around the print
                          ``(post_close_1d - pre_close) / pre_close``
- ``realized_move_5d``  - the 5-trading-day forward move from pre_close
- ``abs_move_1d``       - absolute value of the 1-day move
- ``trailing_median_8q`` - median ``abs_move_1d`` of the **8 prior** events
                          for the same ticker (strictly earlier in time)
- ``move_ratio``        - ``abs_move_1d / trailing_median_8q``

Why these specific features
---------------------------
``realized_move_1d`` is the label every backtest depends on. Hit rates,
P&L attribution, score calibration — all need a realized move per event.

``trailing_median_8q`` is the per-ticker baseline. CAT prints with ~3%
moves; ETSY prints with ~12%. Absolute % comparison across tickers is
noise. ``move_ratio`` lets us ask "was this print unusually large *for
this name*?" — that's the question PMs actually ask.

Point-in-time discipline
------------------------
This module is the highest-risk place in the project for look-ahead
bias. We enforce three rules mechanically:

1. ``pre_close`` is sourced from the **strictly prior** trading day.
   Earnings on a Wednesday → pre_close is Tuesday's close.

2. ``post_close_1d`` is the **strictly later** trading day's close.
   BMO earnings on Thursday → post_close_1d is Thursday's close
   (the print already happened by close). AMC earnings on Thursday
   → post_close_1d is Friday's close.

   We don't model BMO vs AMC explicitly here; we use the convention
   "first trading day at or after the event_date". The interaction
   with intraday timing is documented in methodology.md as a known
   approximation. For day-of moves we'd need intraday data which we
   don't have on free tiers.

3. ``trailing_median_8q`` only sees events with
   ``other.event_date < current.event_date``. A regression test
   enforces this (``test_realized_moves.py::test_trailing_median_excludes_self_and_future``).

Storage
-------
Each row goes to the ``realized_moves`` table with ``as_of = computed_at``.
We don't write the table inside a PITContext because realized moves
are an artifact of historical computation, not a live feature lookup.
The downstream backtest will query ``realized_moves`` *with* a PITContext
to filter to data observable at the simulated time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from statistics import median
from typing import TYPE_CHECKING

from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


@dataclass(frozen=True)
class RealizedMove:
    """One row of the realized_moves table."""

    ticker: str
    event_date: date
    pre_close_date: date | None
    post_close_date_1d: date | None
    post_close_date_5d: date | None
    pre_close: float | None
    post_close_1d: float | None
    post_close_5d: float | None
    realized_move_1d: float | None
    realized_move_5d: float | None
    abs_move_1d: float | None
    trailing_median_8q: float | None
    n_prior_events: int
    move_ratio: float | None
    as_of: datetime


# ---------------------------------------------------------------------------
# Price lookups
# ---------------------------------------------------------------------------


def previous_trading_close(
    conn: duckdb.DuckDBPyConnection, ticker: str, target_date: date
) -> tuple[date, float] | None:
    """Return (date, close) of the most recent trading day strictly before
    ``target_date`` for ``ticker``, or None if no such bar exists.

    Uses the latest vintage in the warehouse (newest ``as_of`` per date).
    Backtests at simulated time T should layer their own PIT filter on the
    realized_moves table itself, not here.
    """
    row = conn.execute(
        """
        SELECT date, close
        FROM prices
        WHERE ticker = ? AND date < ?
        ORDER BY date DESC, as_of DESC
        LIMIT 1
        """,
        [ticker, target_date],
    ).fetchone()
    if row is None or row[1] is None:
        return None
    return (row[0], float(row[1]))


def trading_close_at_offset(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    target_date: date,
    *,
    n_trading_days_after: int,
) -> tuple[date, float] | None:
    """Return (date, close) of the Nth trading day at or after ``target_date``.

    ``n_trading_days_after=1`` returns the first trading day on or after
    ``target_date``. ``n_trading_days_after=5`` returns the 5th. We use
    "at or after" semantics here because earnings dates often land on
    trading days; the price for that day's close is the first observable
    close that incorporates the print's information (under our BMO/AMC
    approximation documented above).
    """
    if n_trading_days_after < 1:
        raise ValueError("n_trading_days_after must be >= 1")

    rows = conn.execute(
        """
        SELECT date, close
        FROM prices
        WHERE ticker = ? AND date >= ?
        ORDER BY date ASC, as_of DESC
        LIMIT ?
        """,
        [ticker, target_date, n_trading_days_after * 4],  # buffer for vintage dups
    ).fetchall()

    # Dedup by date keeping the newest vintage (already DESC on as_of within date)
    seen: set[date] = set()
    distinct: list[tuple[date, float]] = []
    for d, c in rows:
        if d in seen or c is None:
            continue
        seen.add(d)
        distinct.append((d, float(c)))

    if len(distinct) < n_trading_days_after:
        return None
    return distinct[n_trading_days_after - 1]


# ---------------------------------------------------------------------------
# Per-event computation
# ---------------------------------------------------------------------------


def compute_event_moves(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
    *,
    as_of: datetime,
) -> RealizedMove:
    """Compute the moves for a single (ticker, event_date).

    The trailing median is NOT computed here — it's a bulk operation
    that's more efficient run in one pass over the universe (see
    ``compute_universe_moves``).
    """
    pre = previous_trading_close(conn, ticker, event_date)
    post1 = trading_close_at_offset(conn, ticker, event_date, n_trading_days_after=1)
    post5 = trading_close_at_offset(conn, ticker, event_date, n_trading_days_after=5)

    pre_close = pre[1] if pre else None
    post_close_1d = post1[1] if post1 else None
    post_close_5d = post5[1] if post5 else None

    realized_1d: float | None = None
    realized_5d: float | None = None
    abs_1d: float | None = None
    if pre_close and post_close_1d and pre_close != 0:
        realized_1d = (post_close_1d - pre_close) / pre_close
        abs_1d = abs(realized_1d)
    if pre_close and post_close_5d and pre_close != 0:
        realized_5d = (post_close_5d - pre_close) / pre_close

    return RealizedMove(
        ticker=ticker,
        event_date=event_date,
        pre_close_date=pre[0] if pre else None,
        post_close_date_1d=post1[0] if post1 else None,
        post_close_date_5d=post5[0] if post5 else None,
        pre_close=pre_close,
        post_close_1d=post_close_1d,
        post_close_5d=post_close_5d,
        realized_move_1d=realized_1d,
        realized_move_5d=realized_5d,
        abs_move_1d=abs_1d,
        trailing_median_8q=None,  # filled by bulk pass
        n_prior_events=0,
        move_ratio=None,
        as_of=as_of,
    )


# ---------------------------------------------------------------------------
# Trailing median (PIT-strict)
# ---------------------------------------------------------------------------


def attach_trailing_medians(moves: list[RealizedMove], *, window: int = 8) -> list[RealizedMove]:
    """For each move, compute the median of ``abs_move_1d`` from the prior
    ``window`` events *strictly earlier in time* for the same ticker.

    Strict ordering by event_date ascending; the current event is never
    in its own window. Events with missing ``abs_move_1d`` are skipped
    when building the window but the row itself still gets a (possibly
    None) median.

    Returns a new list — does not mutate input.
    """
    # Group by ticker; within each, sort by event_date ascending
    by_ticker: dict[str, list[RealizedMove]] = {}
    for m in moves:
        by_ticker.setdefault(m.ticker, []).append(m)

    out: list[RealizedMove] = []
    for ticker, ticker_moves in by_ticker.items():
        ordered = sorted(ticker_moves, key=lambda m: m.event_date)
        # Rolling window of prior abs_move_1d values
        prior_abs: list[float] = []
        for m in ordered:
            n_prior = len(prior_abs)
            window_slice = prior_abs[-window:] if n_prior > 0 else []
            trailing_med: float | None = median(window_slice) if window_slice else None
            move_ratio: float | None = None
            if trailing_med is not None and trailing_med > 0 and m.abs_move_1d is not None:
                move_ratio = m.abs_move_1d / trailing_med

            out.append(
                RealizedMove(
                    ticker=m.ticker,
                    event_date=m.event_date,
                    pre_close_date=m.pre_close_date,
                    post_close_date_1d=m.post_close_date_1d,
                    post_close_date_5d=m.post_close_date_5d,
                    pre_close=m.pre_close,
                    post_close_1d=m.post_close_1d,
                    post_close_5d=m.post_close_5d,
                    realized_move_1d=m.realized_move_1d,
                    realized_move_5d=m.realized_move_5d,
                    abs_move_1d=m.abs_move_1d,
                    trailing_median_8q=trailing_med,
                    n_prior_events=len(window_slice),
                    move_ratio=move_ratio,
                    as_of=m.as_of,
                )
            )

            # Add to rolling history AFTER processing (strict PIT)
            if m.abs_move_1d is not None:
                prior_abs.append(m.abs_move_1d)
        log.debug("trailing_medians_computed", ticker=ticker, n=len(ordered))

    return out


# ---------------------------------------------------------------------------
# Warehouse write
# ---------------------------------------------------------------------------


def upsert_realized_moves(conn: duckdb.DuckDBPyConnection, moves: list[RealizedMove]) -> int:
    """Insert realized moves. Idempotent on (ticker, event_date, as_of).

    A re-run with a later ``as_of`` writes new rows; the latest vintage
    wins for downstream consumers.
    """
    if not moves:
        return 0

    def _to_storage(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts
        return ts.astimezone(UTC).replace(tzinfo=None)

    seen: set[tuple[str, date, datetime]] = set()
    deduped: list[tuple[RealizedMove, datetime]] = []
    for m in moves:
        ts = _to_storage(m.as_of)
        key = (m.ticker, m.event_date, ts)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((m, ts))

    placeholders = ",".join("(?, ?, ?)" for _ in deduped)
    existing_rows = conn.execute(
        f"""
        SELECT ticker, event_date, as_of FROM realized_moves
        WHERE (ticker, event_date, as_of) IN ({placeholders})
        """,
        [v for m, ts in deduped for v in (m.ticker, m.event_date, ts)],
    ).fetchall()
    existing = {(r[0], r[1], r[2]) for r in existing_rows}

    to_insert = [(m, ts) for m, ts in deduped if (m.ticker, m.event_date, ts) not in existing]
    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO realized_moves (
            ticker, event_date,
            pre_close_date, post_close_date_1d, post_close_date_5d,
            pre_close, post_close_1d, post_close_5d,
            realized_move_1d, realized_move_5d, abs_move_1d,
            trailing_median_8q, n_prior_events, move_ratio,
            as_of
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                m.ticker,
                m.event_date,
                m.pre_close_date,
                m.post_close_date_1d,
                m.post_close_date_5d,
                m.pre_close,
                m.post_close_1d,
                m.post_close_5d,
                m.realized_move_1d,
                m.realized_move_5d,
                m.abs_move_1d,
                m.trailing_median_8q,
                m.n_prior_events,
                m.move_ratio,
                ts,
            )
            for m, ts in to_insert
        ],
    )
    log.info(
        "realized_moves_upserted",
        new=len(to_insert),
        skipped=len(deduped) - len(to_insert),
    )
    return len(to_insert)


# ---------------------------------------------------------------------------
# Orchestration: compute for the whole universe
# ---------------------------------------------------------------------------


def compute_universe_moves(
    conn: duckdb.DuckDBPyConnection,
    *,
    only_with_actuals: bool = True,
    window: int = 8,
) -> int:
    """Compute realized moves for every historical earnings event.

    By default, restricts to events with ``eps_actual IS NOT NULL`` — i.e.,
    earnings that have already been reported. Upcoming earnings have no
    realized move yet (the price data isn't there).

    Returns rows written.
    """
    where_clause = "WHERE eps_actual IS NOT NULL" if only_with_actuals else ""
    rows = conn.execute(
        f"""
        SELECT DISTINCT ticker, event_date
        FROM earnings_events
        {where_clause}
        ORDER BY ticker, event_date
        """
    ).fetchall()

    log.info("realized_moves_compute_start", n_events=len(rows))
    as_of = datetime.now(UTC)

    moves: list[RealizedMove] = []
    for ticker, event_date in rows:
        moves.append(compute_event_moves(conn, ticker, event_date, as_of=as_of))

    moves_with_medians = attach_trailing_medians(moves, window=window)
    n_written = upsert_realized_moves(conn, moves_with_medians)
    log.info(
        "realized_moves_compute_done",
        n_events=len(rows),
        n_written=n_written,
        window=window,
    )
    return n_written
