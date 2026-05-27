"""Insider sentiment features (signal-quality).

Built on top of the parsed insider_transactions table (populated by
catalyst_engine.data.insider_bulk).

Key concepts
------------
- We filter to transaction codes P (open-market buy) and S (open-market
  sale). Other codes are compensation noise.
- We exclude 10b5-1 pre-planned trades — these were decided 30-90 days
  ahead and carry no current-moment information.
- We weight by transaction value (shares * price), not just count, so
  a $50M CEO buy dominates a $30K director buy.

Features computed for one (ticker, as_of) pair:

- ``insider_net_buying_usd_30d`` — sum(P value) - sum(S value) by
  officers/directors, excluding 10b5-1, in trailing 30 days
- ``insider_buy_value_30d`` — sum of P values only
- ``insider_sell_value_30d`` — sum of S values only
- ``insider_unique_buyers_60d`` — count of distinct filers with at least
  one P transaction in trailing 60d (cluster signal — Cohen-Malloy-Pomorski)
- ``insider_unique_sellers_60d`` — same for S
- ``insider_buys_count_30d`` — count of P transactions, any size
- ``insider_sells_count_30d`` — count of S transactions

PIT discipline
--------------
All windows are strictly less-than ``as_of``. The replay's feature
builder passes the event's score_as_of (end-of-day before event_date).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


def _as_of_naive(as_of: datetime) -> datetime:
    """DuckDB stores transaction times tz-naive; normalize the comparison."""
    return as_of.replace(tzinfo=None) if as_of.tzinfo else as_of


# ---------------------------------------------------------------------------
# Aggregate features
# ---------------------------------------------------------------------------


def insider_window_stats(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
    *,
    window_days: int = 30,
    exclude_10b5_1: bool = True,
) -> dict[str, float | int]:
    """Return aggregate buy/sell stats over the trailing window."""
    as_of_naive = _as_of_naive(as_of)
    code_filter = "transaction_code IN ('P', 'S')"
    plan_filter = "AND COALESCE(is_10b5_1, FALSE) = FALSE" if exclude_10b5_1 else ""

    row = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN transaction_code = 'P' THEN value_usd END), 0) AS buy_value,
          COALESCE(SUM(CASE WHEN transaction_code = 'S' THEN value_usd END), 0) AS sell_value,
          COUNT(*) FILTER (WHERE transaction_code = 'P') AS buy_count,
          COUNT(*) FILTER (WHERE transaction_code = 'S') AS sell_count
        FROM insider_transactions
        WHERE ticker = ?
          AND transaction_date < ?
          AND transaction_date >= ? - INTERVAL '{int(window_days)} days'
          AND {code_filter}
          {plan_filter}
        """,
        [ticker, as_of_naive.date(), as_of_naive.date()],
    ).fetchone()

    buy_value = float(row[0]) if row and row[0] is not None else 0.0
    sell_value = float(row[1]) if row and row[1] is not None else 0.0
    buy_count = int(row[2]) if row and row[2] is not None else 0
    sell_count = int(row[3]) if row and row[3] is not None else 0

    return {
        "insider_buy_value_30d": buy_value,
        "insider_sell_value_30d": sell_value,
        "insider_net_buying_usd_30d": buy_value - sell_value,
        "insider_buys_count_30d": buy_count,
        "insider_sells_count_30d": sell_count,
    }


def insider_unique_actors(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
    *,
    window_days: int = 60,
    exclude_10b5_1: bool = True,
) -> dict[str, int]:
    """Number of distinct insiders who bought or sold in the window.

    Cluster buying (multiple distinct insiders independently buying in a
    short window) is the Cohen-Malloy-Pomorski "opportunistic insider"
    signal — the strongest published insider edge.
    """
    as_of_naive = _as_of_naive(as_of)
    plan_filter = "AND COALESCE(is_10b5_1, FALSE) = FALSE" if exclude_10b5_1 else ""

    row = conn.execute(
        f"""
        SELECT
          COUNT(DISTINCT filer_name) FILTER (WHERE transaction_code = 'P')
            AS unique_buyers,
          COUNT(DISTINCT filer_name) FILTER (WHERE transaction_code = 'S')
            AS unique_sellers
        FROM insider_transactions
        WHERE ticker = ?
          AND transaction_date < ?
          AND transaction_date >= ? - INTERVAL '{int(window_days)} days'
          AND transaction_code IN ('P', 'S')
          {plan_filter}
        """,
        [ticker, as_of_naive.date(), as_of_naive.date()],
    ).fetchone()

    return {
        "insider_unique_buyers_60d": int(row[0]) if row and row[0] is not None else 0,
        "insider_unique_sellers_60d": int(row[1]) if row and row[1] is not None else 0,
    }


# ---------------------------------------------------------------------------
# Unified feature builder
# ---------------------------------------------------------------------------


def build_insider_features(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
) -> dict[str, Any]:
    """Compute all insider features for one (ticker, as_of)."""
    win30 = insider_window_stats(conn, ticker, as_of, window_days=30)
    actors60 = insider_unique_actors(conn, ticker, as_of, window_days=60)
    return {**win30, **actors60}
