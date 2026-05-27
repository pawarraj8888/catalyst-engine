"""Positioning features.

For an upcoming earnings event at date T, we want to know:
- Is the short interest unusually elevated for THIS ticker right now?
  -> high SI z-score => squeeze risk on a beat
- Has insider activity (Form 4 filings) clustered recently?
  -> insiders selling before an event = bearish signal
  -> insiders buying before an event = bullish signal
- Are institutions rotating into / out of the name?
  -> 13F-HR filing count is a coarse proxy until we parse holdings

PIT discipline
--------------
Every feature is computed strictly from data observable at T-1 (the day
before the event). Functions take ``as_of`` explicitly and filter
upstream tables to records dated <= as_of. The replay's feature builder
passes the event's score_as_of.

V0 caveats
----------
- ``form4_count_30d`` and ``form13f_count_90d`` count filings, not
  parsed transactions. A future iteration will parse Form 4 XML for
  signed shares / value, and 13F XML for holding deltas.
- ``si_zscore_1y`` requires us to accumulate weekly SI snapshots over
  time. On a fresh database, the z-score is None (no history). After
  ~12 weeks the z-score becomes meaningful.
"""

from __future__ import annotations

from datetime import datetime
from statistics import mean, stdev
from typing import TYPE_CHECKING, Any

from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Short interest features
# ---------------------------------------------------------------------------


def short_interest_zscore(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
    *,
    lookback_days: int = 365,
    min_observations: int = 6,
) -> tuple[float | None, int]:
    """Z-score of the most recent SI vs trailing 1y of the same ticker's SI.

    Returns (z_score, n_observations). z is None when we don't have at
    least ``min_observations`` historical points to compute a meaningful
    baseline (FINRA reports bi-monthly, so 6 obs ~= 3 months of history).

    Z-score interpretation:
    - z > 1.5  -> SI is meaningfully above its trailing average (crowded)
    - z < -1.5 -> SI has come off sharply (covering)
    - |z| < 1  -> SI in normal range
    """
    as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
    rows = conn.execute(
        f"""
        SELECT settlement_date, short_interest
        FROM short_interest
        WHERE ticker = ?
          AND as_of <= ?
          AND settlement_date >= ? - INTERVAL '{int(lookback_days)} days'
          AND short_interest IS NOT NULL
        ORDER BY settlement_date DESC
        """,
        [ticker, as_of_naive, as_of_naive.date()],
    ).fetchall()

    if len(rows) < min_observations:
        return (None, len(rows))

    values = [float(r[1]) for r in rows]
    most_recent = values[0]
    historical = values[1:]  # exclude the most recent from the baseline

    if len(historical) < 2:
        return (None, len(rows))

    mu = mean(historical)
    sigma = stdev(historical)
    if sigma == 0:
        return (0.0, len(rows))

    z = (most_recent - mu) / sigma
    return (z, len(rows))


def short_interest_latest(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
) -> dict[str, float | None]:
    """Most recent SI snapshot's raw metrics, as observable at as_of."""
    as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
    row = conn.execute(
        """
        SELECT short_interest, avg_daily_volume, days_to_cover, pct_float
        FROM short_interest
        WHERE ticker = ? AND as_of <= ?
        ORDER BY settlement_date DESC, as_of DESC
        LIMIT 1
        """,
        [ticker, as_of_naive],
    ).fetchone()
    if row is None:
        return {
            "short_interest": None,
            "avg_daily_volume": None,
            "days_to_cover": None,
            "pct_float": None,
        }
    return {
        "short_interest": float(row[0]) if row[0] is not None else None,
        "avg_daily_volume": float(row[1]) if row[1] is not None else None,
        "days_to_cover": float(row[2]) if row[2] is not None else None,
        "pct_float": float(row[3]) if row[3] is not None else None,
    }


# ---------------------------------------------------------------------------
# Insider activity (Form 4 count proxy)
# ---------------------------------------------------------------------------


def form4_recent_count(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
    *,
    window_days: int = 30,
) -> int:
    """Count of Form 4 filings for ticker in the trailing window_days
    strictly before as_of.

    Form 4s cluster on:
    - 10b5-1 trade execution days
    - Earnings windows (insiders often sell into strength)
    - Major M&A or strategic announcements

    A spike vs typical baseline is the signal. V0 just returns the count;
    z-score against a trailing baseline is Phase 2 next step.
    """
    # DuckDB stores filed_at tz-naive (we strip on ingest), so normalize
    # the query parameter to match — otherwise tz-aware vs tz-naive
    # comparison yields no matches.
    as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM filings
        WHERE ticker = ?
          AND filing_type = '4'
          AND filed_at < ?
          AND filed_at >= ? - INTERVAL '{int(window_days)} days'
        """,
        [ticker, as_of_naive, as_of_naive],
    ).fetchone()
    return int(row[0]) if row else 0


def form4_baseline_zscore(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
    *,
    window_days: int = 30,
    baseline_quarters: int = 4,
) -> tuple[float | None, int]:
    """Z-score of trailing 30-day Form 4 count vs the prior ~4 quarters
    of 30-day rolling counts for the same ticker.

    Returns (z, n_baseline_periods). z=None when baseline is sparse.
    """
    as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
    # Take 4 non-overlapping prior windows of `window_days` each
    baseline_counts: list[int] = []
    for q in range(1, baseline_quarters + 1):
        offset_start = window_days * (q + 1)
        offset_end = window_days * q
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM filings
            WHERE ticker = ?
              AND filing_type = '4'
              AND filed_at < ? - INTERVAL '{int(offset_end)} days'
              AND filed_at >= ? - INTERVAL '{int(offset_start)} days'
            """,
            [ticker, as_of_naive, as_of_naive],
        ).fetchone()
        baseline_counts.append(int(row[0]) if row else 0)

    if len(baseline_counts) < 2:
        return (None, len(baseline_counts))

    current = form4_recent_count(conn, ticker, as_of, window_days=window_days)
    mu = mean(baseline_counts)
    sigma = stdev(baseline_counts)
    if sigma == 0:
        return (0.0, len(baseline_counts))
    z = (current - mu) / sigma
    return (z, len(baseline_counts))


# ---------------------------------------------------------------------------
# 13F clustering
# ---------------------------------------------------------------------------


def form13f_recent_count(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
    *,
    window_days: int = 90,
) -> int:
    """Count of 13F-HR filings mentioning this ticker in the trailing window.

    13F-HRs are filed quarterly by institutions with $100M+ AUM. A spike
    above baseline means new institutions have been initiating positions
    (or existing ones increasing). The opposite (count drop) signals
    rotation out.

    Note: we filter by `ticker` on the filings table, but 13F-HRs are
    filed by the *holder*, not the ticker. The ticker on these rows is
    typically NULL. So this currently returns 0 — it's a placeholder
    until we parse the 13F-HR holdings XML in a future module.
    """
    as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM filings
        WHERE ticker = ?
          AND filing_type = '13F-HR'
          AND filed_at < ?
          AND filed_at >= ? - INTERVAL '{int(window_days)} days'
        """,
        [ticker, as_of_naive, as_of_naive],
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Unified positioning feature builder
# ---------------------------------------------------------------------------


def build_positioning_features(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of: datetime,
) -> dict[str, Any]:
    """Compute all positioning features for one (ticker, as_of) pair.

    Returns a dict suitable for merging into the replay's feature dict.
    All keys present; values may be None when underlying data is missing.
    """
    si_z, si_n = short_interest_zscore(conn, ticker, as_of)
    si_latest = short_interest_latest(conn, ticker, as_of)
    f4_recent = form4_recent_count(conn, ticker, as_of)
    f4_z, _f4_n = form4_baseline_zscore(conn, ticker, as_of)
    f13f_recent = form13f_recent_count(conn, ticker, as_of)

    return {
        # Short interest
        "si_zscore_1y": si_z,
        "si_n_observations": si_n,
        "si_days_to_cover": si_latest["days_to_cover"],
        "si_pct_float": si_latest["pct_float"],
        # Insider activity
        "form4_count_30d": f4_recent,
        "form4_zscore_4q": f4_z,
        # Institutional activity
        "form13f_count_90d": f13f_recent,
    }
