"""Tests for backtest replay — the highest PIT-risk module in Phase 3."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import pytest

from catalyst_engine.backtest.replay import (
    build_features_for_event,
    fetch_event_label,
    is_hit,
    replay,
    upsert_scored_setups,
)
from catalyst_engine.scoring.scorer import ScoringConfig, ScoringRule

UTC = UTC


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _insert_realized_move(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    event_date: date,
    abs_move: float | None,
    ratio: float | None,
    trail_median: float | None,
    realized_1d: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO realized_moves (
            ticker, event_date, pre_close_date, post_close_date_1d, post_close_date_5d,
            pre_close, post_close_1d, post_close_5d,
            realized_move_1d, realized_move_5d, abs_move_1d,
            trailing_median_8q, n_prior_events, move_ratio, as_of
        ) VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL,
                  ?, NULL, ?, ?, 0, ?, ?)
        """,
        [
            ticker,
            event_date,
            realized_1d,
            abs_move,
            trail_median,
            ratio,
            datetime(event_date.year, event_date.month, event_date.day),
        ],
    )


def _insert_earnings(
    conn: duckdb.DuckDBPyConnection,
    rows: list[tuple[str, date]],
) -> None:
    conn.executemany(
        """
        INSERT INTO earnings_events
        (ticker, event_date, time_of_day, eps_actual, as_of, source)
        VALUES (?, ?, 'UNK', 1.0, ?, 'test')
        """,
        [(t, d, datetime(d.year, d.month, d.day)) for t, d in rows],
    )


# ---------------------------------------------------------------------------
# build_features_for_event — PIT correctness
# ---------------------------------------------------------------------------


def test_features_excludes_current_event(warehouse: duckdb.DuckDBPyConnection) -> None:
    """The event being scored MUST NOT be in its own features."""
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=date(2024, 1, 1),
        abs_move=0.02,
        ratio=0.5,
        trail_median=0.04,
    )
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=date(2024, 4, 1),
        abs_move=0.15,
        ratio=3.0,
        trail_median=0.05,
    )

    # Features for the April event should see ONLY the January row
    features = build_features_for_event(warehouse, "AAPL", date(2024, 4, 1))
    assert features["n_prior_events"] == 1
    assert features["last_ratio"] == 0.5


def test_features_excludes_future_events(warehouse: duckdb.DuckDBPyConnection) -> None:
    """A later event must not appear in the scoring features."""
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=date(2024, 1, 1),
        abs_move=0.02,
        ratio=0.5,
        trail_median=0.04,
    )
    _insert_realized_move(  # later event
        warehouse,
        ticker="AAPL",
        event_date=date(2024, 7, 1),
        abs_move=0.10,
        ratio=2.0,
        trail_median=0.05,
    )

    features = build_features_for_event(warehouse, "AAPL", date(2024, 4, 1))
    assert features["n_prior_events"] == 1
    assert features["last_ratio"] == 0.5  # NOT 2.0


def test_features_with_no_history(warehouse: duckdb.DuckDBPyConnection) -> None:
    features = build_features_for_event(warehouse, "IPO", date(2025, 1, 1))
    assert features["n_prior_events"] == 0
    assert features["last_ratio"] is None
    assert features["trailing_3q_median_ratio"] is None


def test_trailing_3q_median_ratio_computed(warehouse: duckdb.DuckDBPyConnection) -> None:
    """When 3+ prior ratios exist, trailing_3q_median_ratio is their median."""
    for i, ratio in enumerate([0.3, 0.4, 0.5, 0.6, 0.7]):
        _insert_realized_move(
            warehouse,
            ticker="X",
            event_date=date(2024, i + 1, 1),
            abs_move=0.03,
            ratio=ratio,
            trail_median=0.05,
        )
    # Event in July sees prior ratios [0.7, 0.6, 0.5, 0.4, 0.3] (newest first)
    # Most recent 3 = [0.7, 0.6, 0.5] -> median 0.6
    features = build_features_for_event(warehouse, "X", date(2024, 7, 1))
    assert features["trailing_3q_median_ratio"] == pytest.approx(0.6)


def test_trailing_3q_all_below_05_flag(warehouse: duckdb.DuckDBPyConnection) -> None:
    for i, ratio in enumerate([0.3, 0.4, 0.45]):
        _insert_realized_move(
            warehouse,
            ticker="X",
            event_date=date(2024, i + 1, 1),
            abs_move=0.02,
            ratio=ratio,
            trail_median=0.05,
        )
    features = build_features_for_event(warehouse, "X", date(2024, 7, 1))
    assert features["trailing_3q_all_below_05"] is True

    # Now add one larger ratio in the most-recent slot
    _insert_realized_move(
        warehouse,
        ticker="X",
        event_date=date(2024, 6, 1),
        abs_move=0.05,
        ratio=1.5,
        trail_median=0.05,
    )
    features = build_features_for_event(warehouse, "X", date(2024, 7, 1))
    assert features["trailing_3q_all_below_05"] is False


# ---------------------------------------------------------------------------
# is_hit and label fetch
# ---------------------------------------------------------------------------


def test_is_hit_basic() -> None:
    assert is_hit(0.10, 0.05) is True  # 10% > 5% baseline -> hit
    assert is_hit(0.02, 0.05) is False  # 2% < 5% -> miss
    assert is_hit(None, 0.05) is None
    assert is_hit(0.10, None) is None


def test_fetch_event_label(warehouse: duckdb.DuckDBPyConnection) -> None:
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=date(2024, 1, 1),
        abs_move=0.10,
        ratio=2.5,
        trail_median=0.04,
        realized_1d=0.10,
    )
    label = fetch_event_label(warehouse, "AAPL", date(2024, 1, 1))
    assert label[0] == pytest.approx(0.10)
    assert label[1] == pytest.approx(0.10)
    assert label[2] == pytest.approx(0.04)
    assert label[3] == pytest.approx(2.5)


def test_fetch_event_label_missing(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert fetch_event_label(warehouse, "GHOST", date(2024, 1, 1)) == (None, None, None, None)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def test_upsert_empty_is_noop(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert upsert_scored_setups(warehouse, []) == 0


# ---------------------------------------------------------------------------
# End-to-end replay
# ---------------------------------------------------------------------------


def _trivial_config() -> ScoringConfig:
    return ScoringConfig(
        version=1,
        high_conviction_threshold=5.0,
        rules_by_catalyst={
            "earnings": [
                ScoringRule(
                    name="has_history",
                    description="",
                    condition="n_prior_events >= 1",
                    weight=3.0,
                ),
                ScoringRule(
                    name="vol_compression",
                    description="",
                    condition="last_ratio is not None and last_ratio < 0.7",
                    weight=4.0,
                ),
            ]
        },
    )


def test_replay_end_to_end(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Insert 3 earnings, 3 realized_moves, run replay, verify scored_setups
    has 3 rows with sensible scores and labels."""
    # Three quarterly events
    dates = [date(2024, 1, 30), date(2024, 4, 30), date(2024, 7, 31)]
    _insert_earnings(warehouse, [("AAPL", d) for d in dates])

    # Each event has a realized_move. The 2nd and 3rd benefit from history.
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=dates[0],
        abs_move=0.03,
        ratio=None,
        trail_median=None,
        realized_1d=0.03,
    )
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=dates[1],
        abs_move=0.02,
        ratio=0.5,
        trail_median=0.04,
        realized_1d=-0.02,
    )
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=dates[2],
        abs_move=0.08,
        ratio=2.0,
        trail_median=0.04,
        realized_1d=0.08,
    )

    run_id, n = replay(warehouse, config=_trivial_config())
    assert n == 3

    rows = warehouse.execute(
        "SELECT event_date, score, is_hit FROM scored_setups WHERE run_id = ? ORDER BY event_date",
        [str(run_id)],
    ).fetchall()
    assert len(rows) == 3

    # First event has no prior history => score 0
    assert float(rows[0][1]) == 0.0
    # Second event: 1 prior (history rule fires), last_ratio not yet computable
    #   from realized_moves where ratio was None on event 1
    # So vol_compression doesn't fire, only has_history -> 3.0
    assert float(rows[1][1]) == 3.0
    # Third event: 2 priors, last_ratio = 0.5 -> both rules fire -> 7.0
    assert float(rows[2][1]) == 7.0

    # Labels populated correctly: event 3 was a hit (0.08 > 0.04 baseline)
    assert rows[2][2] is True
    # Event 2 was a miss (0.02 < 0.04)
    assert rows[1][2] is False


def test_replay_respects_date_window(warehouse: duckdb.DuckDBPyConnection) -> None:
    dates = [date(2023, 1, 30), date(2024, 1, 30), date(2025, 1, 30)]
    _insert_earnings(warehouse, [("AAPL", d) for d in dates])
    for d in dates:
        _insert_realized_move(
            warehouse,
            ticker="AAPL",
            event_date=d,
            abs_move=0.05,
            ratio=1.0,
            trail_median=0.04,
            realized_1d=0.05,
        )

    _, n = replay(
        warehouse,
        config=_trivial_config(),
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert n == 1


def test_replay_re_run_produces_new_rows(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Running replay twice must NOT dedup to zero on the second pass.

    Regression for the bug where score_as_of was deterministic (event_date - 1),
    causing every re-run to see "existing" keys and write 0 rows.
    """
    dates = [date(2024, 1, 30), date(2024, 4, 30)]
    _insert_earnings(warehouse, [("AAPL", d) for d in dates])
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=dates[0],
        abs_move=0.03,
        ratio=None,
        trail_median=None,
        realized_1d=0.03,
    )
    _insert_realized_move(
        warehouse,
        ticker="AAPL",
        event_date=dates[1],
        abs_move=0.05,
        ratio=1.0,
        trail_median=0.03,
        realized_1d=0.05,
    )

    # First run
    run_id_1, n1 = replay(warehouse, config=_trivial_config())
    assert n1 == 2

    # Second run - must also produce 2 rows (different run_id, different score_as_of)
    run_id_2, n2 = replay(warehouse, config=_trivial_config())
    assert n2 == 2, "Re-running replay must not dedup to zero"
    assert run_id_1 != run_id_2

    # Both runs are in the warehouse
    total = warehouse.execute("SELECT COUNT(*) FROM scored_setups").fetchone()
    assert total[0] == 4
