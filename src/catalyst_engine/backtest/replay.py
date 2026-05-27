"""Backtest replay.

For every historical earnings event in the warehouse, build the feature
dict *as it would have looked* the day before the event, score it, and
write the result to `scored_setups`.

PIT discipline
--------------
For an event at date T, we evaluate at `as_of = T - 1 day, 23:59:59 UTC`.
This means:
- Trailing realized moves come from events strictly before T
- The current event's own realized move is the LABEL, never a feature
- If a ticker has fewer than `min_prior_events` prior events, we still
  score it (rules can no-op), but flag with `n_prior_events < min`

Walk-forward (Phase 3.5+)
-------------------------
V0 does a single in-sample pass — every event scored once with rules
fixed. Phase 3.5 introduces walk-forward refit (quarterly), where rule
weights get recalibrated on the prior quarter's results. The scoring
framework supports this naturally because rules are config, not code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from catalyst_engine.scoring.scorer import ScoringConfig, score_setup
from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


@dataclass(frozen=True)
class ScoredSetupRow:
    """One row destined for the scored_setups table."""

    ticker: str
    event_date: date
    catalyst_type: str
    score: float
    rules_fired: list[str]
    score_components: dict[str, float]
    realized_move_1d: float | None
    abs_move_1d: float | None
    trailing_median: float | None
    move_ratio: float | None
    is_hit: bool | None
    score_as_of: datetime
    run_id: uuid.UUID


# ---------------------------------------------------------------------------
# Feature builder — pulls everything strictly PRIOR to the event date
# ---------------------------------------------------------------------------


def build_features_for_event(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
) -> dict[str, Any]:
    """Build the feature dict for a (ticker, event_date) using only prior events.

    Returns a dict consumed by the scoring engine. All keys present even
    when the underlying value is None — that makes rule conditions like
    `x is not None and x < 0.7` work without KeyError.
    """
    # Pull all prior realized_moves rows for this ticker, strictly before T
    prior = conn.execute(
        """
        SELECT event_date, abs_move_1d, move_ratio, trailing_median_8q
        FROM realized_moves
        WHERE ticker = ? AND event_date < ?
        ORDER BY event_date DESC
        """,
        [ticker, event_date],
    ).fetchall()

    # Extract usable ratios (skip Nones)
    prior_ratios: list[float] = [float(row[2]) for row in prior if row[2] is not None]
    prior_abs_moves: list[float] = [float(row[1]) for row in prior if row[1] is not None]
    prior_medians: list[float] = [float(row[3]) for row in prior if row[3] is not None]

    last_3_ratios = prior_ratios[:3]  # most recent first
    trailing_3q_median_ratio = (
        sorted(last_3_ratios)[len(last_3_ratios) // 2] if len(last_3_ratios) >= 3 else None
    )
    trailing_3q_all_below_05 = len(last_3_ratios) >= 3 and all(r < 0.5 for r in last_3_ratios)
    last_ratio = prior_ratios[0] if prior_ratios else None
    # Trailing median for the upcoming event = the median feature from the
    # most recent prior event (i.e. what we knew going in)
    trailing_median = prior_medians[0] if prior_medians else None

    return {
        "ticker": ticker,
        "n_prior_events": len(prior),
        "trailing_3q_median_ratio": trailing_3q_median_ratio,
        "trailing_3q_all_below_05": trailing_3q_all_below_05,
        "last_ratio": last_ratio,
        "trailing_median": trailing_median,
        "median_prior_abs_move": (
            sorted(prior_abs_moves)[len(prior_abs_moves) // 2] if prior_abs_moves else None
        ),
    }


# ---------------------------------------------------------------------------
# Label lookup — for evaluation only, NEVER fed into features
# ---------------------------------------------------------------------------


def fetch_event_label(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (realized_move_1d, abs_move_1d, trailing_median, move_ratio).

    Called after scoring. These are the labels that backtest metrics use;
    they MUST NOT be in the feature dict.
    """
    row = conn.execute(
        """
        SELECT realized_move_1d, abs_move_1d, trailing_median_8q, move_ratio
        FROM realized_moves
        WHERE ticker = ? AND event_date = ?
        ORDER BY as_of DESC
        LIMIT 1
        """,
        [ticker, event_date],
    ).fetchone()
    if row is None:
        return (None, None, None, None)
    return (
        float(row[0]) if row[0] is not None else None,
        float(row[1]) if row[1] is not None else None,
        float(row[2]) if row[2] is not None else None,
        float(row[3]) if row[3] is not None else None,
    )


def is_hit(abs_move_1d: float | None, trailing_median: float | None) -> bool | None:
    """A setup is a hit when the realized abs move > trailing baseline.

    Returns None when either value is missing — the event can't be evaluated.
    Not directional; directional hits require positioning/skew which arrive
    in Phase 2.
    """
    if abs_move_1d is None or trailing_median is None:
        return None
    return abs_move_1d > trailing_median


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay(
    conn: duckdb.DuckDBPyConnection,
    *,
    config: ScoringConfig,
    catalyst_type: str = "earnings",
    start: date | None = None,
    end: date | None = None,
) -> tuple[uuid.UUID, int]:
    """Walk every historical earnings event in the warehouse, score it,
    write the row to scored_setups.

    Returns (run_id, n_rows_written).
    """
    run_id = uuid.uuid4()
    log.info("replay_start", run_id=str(run_id), catalyst_type=catalyst_type)

    where_clauses = ["eps_actual IS NOT NULL"]
    params: list[Any] = []
    if start is not None:
        where_clauses.append("event_date >= ?")
        params.append(start)
    if end is not None:
        where_clauses.append("event_date <= ?")
        params.append(end)
    where_sql = " AND ".join(where_clauses)

    events = conn.execute(
        f"""
        SELECT DISTINCT ticker, event_date
        FROM earnings_events
        WHERE {where_sql}
        ORDER BY event_date, ticker
        """,
        params,
    ).fetchall()

    log.info("replay_events_loaded", n=len(events))

    # Single run-time `score_as_of` for the whole replay. The per-event PIT
    # discipline is enforced by build_features_for_event (which filters to
    # event_date < current event); `score_as_of` is when WE computed the score,
    # not when we pretended to be. Using a fixed run-time stamp lets us
    # re-run replays without dedup collisions on identical event keys.
    score_as_of = datetime.now(UTC)

    rows_to_insert: list[ScoredSetupRow] = []
    for ticker, event_date in events:
        features = build_features_for_event(conn, ticker, event_date)
        result = score_setup(features, catalyst_type=catalyst_type, config=config)

        # Labels — fetched AFTER scoring, used only for evaluation
        realized_1d, abs_1d, trail_med, ratio = fetch_event_label(conn, ticker, event_date)
        hit = is_hit(abs_1d, trail_med)

        rows_to_insert.append(
            ScoredSetupRow(
                ticker=ticker,
                event_date=event_date,
                catalyst_type=catalyst_type,
                score=result.score,
                rules_fired=result.rules_fired,
                score_components=result.score_components,
                realized_move_1d=realized_1d,
                abs_move_1d=abs_1d,
                trailing_median=trail_med,
                move_ratio=ratio,
                is_hit=hit,
                score_as_of=score_as_of,
                run_id=run_id,
            )
        )

    n_written = upsert_scored_setups(conn, rows_to_insert)
    log.info("replay_done", run_id=str(run_id), n=n_written)
    return run_id, n_written


def upsert_scored_setups(conn: duckdb.DuckDBPyConnection, rows: list[ScoredSetupRow]) -> int:
    """Insert scored setup rows. Idempotent on (ticker, event_date,
    catalyst_type, score_as_of)."""
    if not rows:
        return 0

    import json

    def _ts(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts
        return ts.astimezone(UTC).replace(tzinfo=None)

    # Dedup within the batch
    seen: set[tuple[str, date, str, datetime]] = set()
    deduped: list[tuple[ScoredSetupRow, datetime]] = []
    for r in rows:
        ts = _ts(r.score_as_of)
        key = (r.ticker, r.event_date, r.catalyst_type, ts)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((r, ts))

    placeholders = ",".join("(?, ?, ?, ?)" for _ in deduped)
    existing = conn.execute(
        f"""
        SELECT ticker, event_date, catalyst_type, score_as_of FROM scored_setups
        WHERE (ticker, event_date, catalyst_type, score_as_of) IN ({placeholders})
        """,
        [v for r, ts in deduped for v in (r.ticker, r.event_date, r.catalyst_type, ts)],
    ).fetchall()
    existing_keys = {(row[0], row[1], row[2], row[3]) for row in existing}

    to_insert = [
        (r, ts)
        for r, ts in deduped
        if (r.ticker, r.event_date, r.catalyst_type, ts) not in existing_keys
    ]
    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO scored_setups (
            ticker, event_date, catalyst_type, score, rules_fired,
            score_components, realized_move_1d, abs_move_1d, trailing_median,
            move_ratio, is_hit, score_as_of, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.ticker,
                r.event_date,
                r.catalyst_type,
                r.score,
                r.rules_fired,
                json.dumps(r.score_components),
                r.realized_move_1d,
                r.abs_move_1d,
                r.trailing_median,
                r.move_ratio,
                r.is_hit,
                ts,
                str(r.run_id),
            )
            for r, ts in to_insert
        ],
    )
    log.info("scored_setups_upserted", new=len(to_insert))
    return len(to_insert)
