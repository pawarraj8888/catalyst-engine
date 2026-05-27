"""Live calls loop.

The point of this module
------------------------
The backtest tells us what the rules WOULD have done historically.
The live loop generates real-time, time-stamped, public calls that
accumulate into a verifiable track record. Anyone reading the repo
in 3 months sees N rows in live_log/calls.csv, each with a real
timestamp, the score we gave it, what the realized move actually
was, and a post-mortem note if it missed.

Why this matters more than the backtest
---------------------------------------
On free large-cap data, the V0 backtest comes out at ~50%. That's
not a flaw of the project — that's the efficiency of the market.
The asymmetric value is in the public track record: a PM evaluating
this artifact can see whether the engine's calls actually played
out, whether the post-mortems are honest, and whether the discipline
is sustained over time.

Two commands
------------
``catalyst live scan``    — Generate calls for upcoming events.
                            Idempotent on (ticker, event_date).
``catalyst live resolve`` — Fill in realized outcomes for past calls.

Outputs
-------
live_log/calls.csv               — Append-only CSV of every call
live_log/postmortems/{date}-{ticker}.md  — One file per MISS or
                                            INVALIDATED call
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from catalyst_engine.backtest.replay import build_features_for_event
from catalyst_engine.config import get_settings
from catalyst_engine.features.realized_moves import (
    previous_trading_close,
    trading_close_at_offset,
)
from catalyst_engine.scoring.scorer import ScoringConfig, score_setup
from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# CSV column contract — DO NOT CHANGE without bumping `schema_version`
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "timestamp",  # ISO-8601 UTC, when this row was first written
    "schema_version",  # bump on breaking changes
    "ticker",
    "catalyst_type",  # earnings | 8k | fda | guidance
    "event_date",  # date the catalyst lands
    "score",  # 0-10 from scoring engine
    "rules_fired",  # pipe-separated rule names
    "score_components",  # JSON: {rule_name: weight}
    "trailing_baseline_pct",  # the historical baseline move for context
    "outcome",  # PENDING | HIT | MISS | INVALIDATED
    "realized_move_pct",  # 1-day move from pre-close to post-close (signed)
    "abs_move_pct",  # absolute value
    "move_ratio",  # abs_move / trailing_baseline
    "resolved_at",  # ISO-8601 UTC when outcome was filled in
    "note",  # one-liner post-mortem when outcome != HIT
]

CURRENT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class LiveCall:
    """One row of live_log/calls.csv."""

    timestamp: str
    schema_version: str
    ticker: str
    catalyst_type: str
    event_date: date
    score: float
    rules_fired: list[str]
    score_components: dict[str, float]
    trailing_baseline_pct: float | None
    outcome: str = "PENDING"
    realized_move_pct: float | None = None
    abs_move_pct: float | None = None
    move_ratio: float | None = None
    resolved_at: str | None = None
    note: str | None = None

    def to_row(self) -> dict[str, str]:
        """Serialize to dict suitable for csv.DictWriter."""

        def _fmt_float(x: float | None, digits: int = 4) -> str:
            return "" if x is None else f"{x:.{digits}f}"

        return {
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
            "ticker": self.ticker,
            "catalyst_type": self.catalyst_type,
            "event_date": self.event_date.isoformat(),
            "score": f"{self.score:.2f}",
            "rules_fired": "|".join(self.rules_fired),
            "score_components": json.dumps(self.score_components, sort_keys=True),
            "trailing_baseline_pct": _fmt_float(self.trailing_baseline_pct),
            "outcome": self.outcome,
            "realized_move_pct": _fmt_float(self.realized_move_pct),
            "abs_move_pct": _fmt_float(self.abs_move_pct),
            "move_ratio": _fmt_float(self.move_ratio, digits=2),
            "resolved_at": self.resolved_at or "",
            "note": self.note or "",
        }

    @classmethod
    def from_row(cls, row: dict[str, str]) -> LiveCall:
        def _parse_float(s: str) -> float | None:
            s = (s or "").strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        def _parse_components(s: str) -> dict[str, float]:
            s = (s or "").strip()
            if not s:
                return {}
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return {}

        return cls(
            timestamp=row.get("timestamp", ""),
            schema_version=row.get("schema_version", "1"),
            ticker=row.get("ticker", "").upper(),
            catalyst_type=row.get("catalyst_type", ""),
            event_date=date.fromisoformat(row["event_date"]),
            score=float(row.get("score", "0") or "0"),
            rules_fired=[r for r in (row.get("rules_fired") or "").split("|") if r],
            score_components=_parse_components(row.get("score_components", "")),
            trailing_baseline_pct=_parse_float(row.get("trailing_baseline_pct", "")),
            outcome=row.get("outcome", "PENDING") or "PENDING",
            realized_move_pct=_parse_float(row.get("realized_move_pct", "")),
            abs_move_pct=_parse_float(row.get("abs_move_pct", "")),
            move_ratio=_parse_float(row.get("move_ratio", "")),
            resolved_at=(row.get("resolved_at") or None),
            note=(row.get("note") or None),
        )


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def _calls_csv_path() -> Path:
    return get_settings().project_root / "live_log" / "calls.csv"


def _ensure_calls_csv() -> Path:
    """Create the calls.csv file with a header if it doesn't exist yet."""
    path = _calls_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
    return path


def read_calls() -> list[LiveCall]:
    """Read every call from live_log/calls.csv.

    Skips comment lines (starting with #) and rows with missing event_date
    (e.g., the original comment-style scaffolding).
    """
    path = _ensure_calls_csv()
    out: list[LiveCall] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            ts = (raw.get("timestamp") or "").strip()
            if ts.startswith("#"):
                continue
            if not (raw.get("event_date") or "").strip():
                continue
            try:
                out.append(LiveCall.from_row(raw))
            except (KeyError, ValueError) as exc:
                log.debug("live_call_skip_bad_row", error=str(exc))
                continue
    return out


def write_calls(calls: list[LiveCall]) -> None:
    """Overwrite calls.csv with the given calls.

    The on-disk file is treated as authoritative for everything we've
    ever logged. To update one call (e.g., during resolve), we read all,
    mutate in memory, then write all back.
    """
    path = _ensure_calls_csv()
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for c in calls:
            writer.writerow(c.to_row())


# ---------------------------------------------------------------------------
# Scan: generate calls for upcoming events
# ---------------------------------------------------------------------------


def find_upcoming_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    today: date | None = None,
    horizon_days: int = 14,
) -> list[tuple[str, date, str]]:
    """Return (ticker, event_date, catalyst_type) for events landing in the
    next ``horizon_days`` days that we don't already have a realized move for.

    "Upcoming" means event_date >= today (inclusive) and <= today+horizon.
    """
    if today is None:
        today = datetime.now(UTC).date()
    horizon = today + timedelta(days=horizon_days)

    rows = conn.execute(
        """
        SELECT DISTINCT ticker, event_date
        FROM earnings_events
        WHERE event_date >= ? AND event_date <= ?
          AND eps_actual IS NULL   -- not yet reported
        ORDER BY event_date, ticker
        """,
        [today, horizon],
    ).fetchall()

    return [(t, d, "earnings") for t, d in rows]


def _scan_call_for_event(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
    catalyst_type: str,
    config: ScoringConfig,
) -> LiveCall:
    """Score one upcoming event and build a LiveCall."""
    features = build_features_for_event(conn, ticker, event_date)
    result = score_setup(features, catalyst_type=catalyst_type, config=config)

    trailing = features.get("trailing_median")
    return LiveCall(
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        schema_version=CURRENT_SCHEMA_VERSION,
        ticker=ticker,
        catalyst_type=catalyst_type,
        event_date=event_date,
        score=result.score,
        rules_fired=result.rules_fired,
        score_components=result.score_components,
        trailing_baseline_pct=(trailing * 100.0 if trailing is not None else None),
        outcome="PENDING",
    )


def scan_and_log(
    conn: duckdb.DuckDBPyConnection,
    *,
    config: ScoringConfig,
    today: date | None = None,
    horizon_days: int = 14,
    min_score: float | None = None,
) -> tuple[int, int]:
    """Scan upcoming events, score each, append new calls to calls.csv.

    Idempotent on (ticker, event_date, catalyst_type). Re-running on the
    same day won't duplicate rows.

    By default we log every upcoming call regardless of score - the public
    track record should show what the engine said for everything, including
    low-score / no-conviction setups. Pass min_score to filter.

    Returns (n_new_calls, n_skipped_existing).
    """
    existing_calls = read_calls()
    existing_keys = {(c.ticker, c.event_date, c.catalyst_type) for c in existing_calls}

    upcoming = find_upcoming_events(conn, today=today, horizon_days=horizon_days)
    log.info("live_scan_start", n_upcoming=len(upcoming), horizon_days=horizon_days)

    new_calls: list[LiveCall] = []
    skipped = 0
    for ticker, event_date, catalyst_type in upcoming:
        if (ticker, event_date, catalyst_type) in existing_keys:
            skipped += 1
            continue
        call = _scan_call_for_event(conn, ticker, event_date, catalyst_type, config)
        if min_score is not None and call.score < min_score:
            continue
        new_calls.append(call)

    if new_calls:
        write_calls(existing_calls + new_calls)
    log.info("live_scan_done", new_calls=len(new_calls), skipped=skipped)
    return (len(new_calls), skipped)


# ---------------------------------------------------------------------------
# Resolve: fill in realized outcomes for past calls
# ---------------------------------------------------------------------------


def _compute_realized_move(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
) -> tuple[float | None, float | None]:
    """Compute (realized_move_pct, abs_move_pct) for an event.

    Mirrors realized_moves.compute_event_moves but doesn't depend on the
    realized_moves table being populated for this specific event (the
    resolve command might run before the next batch feature build).

    Semantic: pre-close = trading day strictly before event_date.
    post-close = first trading day at-or-after event_date (the close that
    incorporates the print under our BMO/AMC approximation).
    """
    pre = previous_trading_close(conn, ticker, event_date)
    post = trading_close_at_offset(conn, ticker, event_date, n_trading_days_after=1)
    if pre is None or post is None or pre[1] == 0:
        return (None, None)
    realized = (post[1] - pre[1]) / pre[1] * 100.0
    return (realized, abs(realized))


def _classify_outcome(
    abs_move_pct: float | None,
    trailing_baseline_pct: float | None,
) -> str:
    """HIT when abs realized move > trailing baseline. MISS otherwise.

    Returns INVALIDATED when we can't compute the outcome (missing prices).
    """
    if abs_move_pct is None or trailing_baseline_pct is None:
        return "INVALIDATED"
    return "HIT" if abs_move_pct > trailing_baseline_pct else "MISS"


def _postmortem_note(call: LiveCall) -> str:
    """One-line note explaining outcome, used in CSV and post-mortem md."""
    if call.outcome == "HIT":
        return (
            f"HIT: scored {call.score:.1f}, realized {call.realized_move_pct:+.2f}% "
            f"(baseline {call.trailing_baseline_pct:.2f}%, "
            f"ratio {call.move_ratio:.2f}x)"
        )
    if call.outcome == "MISS":
        return (
            f"MISS: scored {call.score:.1f}, realized {call.realized_move_pct:+.2f}% "
            f"(baseline {call.trailing_baseline_pct:.2f}%, "
            f"ratio {call.move_ratio:.2f}x)"
        )
    return f"INVALIDATED: could not compute realized move for {call.ticker} {call.event_date}"


def _write_postmortem_file(call: LiveCall) -> Path | None:
    """For MISS or INVALIDATED outcomes, write a markdown post-mortem.

    The file lives at live_log/postmortems/{event_date}-{ticker}.md.
    Format is deliberately concise — a PM should be able to read it in
    20 seconds.
    """
    if call.outcome == "HIT":
        return None

    dir_path = get_settings().project_root / "live_log" / "postmortems"
    dir_path.mkdir(parents=True, exist_ok=True)
    pm_path = dir_path / f"{call.event_date.isoformat()}-{call.ticker}.md"

    rules_md = ", ".join(call.rules_fired) if call.rules_fired else "(none)"
    realized = f"{call.realized_move_pct:+.2f}%" if call.realized_move_pct is not None else "N/A"
    baseline = (
        f"{call.trailing_baseline_pct:.2f}%" if call.trailing_baseline_pct is not None else "N/A"
    )
    move_ratio_str = f"{call.move_ratio:.2f}x" if call.move_ratio is not None else "N/A"

    content = f"""# {call.ticker} {call.event_date.isoformat()} -- {call.outcome}

**Scored:** {call.score:.2f}
**Realized 1d move:** {realized}
**Trailing baseline:** {baseline}
**Move ratio:** {move_ratio_str}

## Rules fired

{rules_md}

## Components

```json
{json.dumps(call.score_components, indent=2, sort_keys=True)}
```

## Note

{call.note}

## What to learn

(Filled in manually after review. The model said this was a {call.score:.1f}-score
setup; the market disagreed. Why?)
"""
    pm_path.write_text(content, encoding="utf-8")
    return pm_path


def resolve_pending_calls(
    conn: duckdb.DuckDBPyConnection,
    *,
    today: date | None = None,
) -> dict[str, int]:
    """Resolve any PENDING calls whose event_date has passed.

    Reads calls.csv, finds rows with outcome=PENDING and event_date < today,
    computes realized moves from the prices table, classifies HIT/MISS/
    INVALIDATED, and writes the file back. Writes a post-mortem markdown
    file for MISSes and INVALIDATEDs.

    Returns counts: {n_resolved, n_hit, n_miss, n_invalidated, n_pending_remaining}.
    """
    if today is None:
        today = datetime.now(UTC).date()

    calls = read_calls()
    stats = {
        "n_resolved": 0,
        "n_hit": 0,
        "n_miss": 0,
        "n_invalidated": 0,
        "n_pending_remaining": 0,
    }
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

    updated: list[LiveCall] = []
    for c in calls:
        if c.outcome != "PENDING":
            updated.append(c)
            continue
        if c.event_date >= today:
            updated.append(c)
            stats["n_pending_remaining"] += 1
            continue

        realized, abs_move = _compute_realized_move(conn, c.ticker, c.event_date)
        outcome = _classify_outcome(abs_move, c.trailing_baseline_pct)
        move_ratio = (
            (abs_move / c.trailing_baseline_pct)
            if (abs_move is not None and c.trailing_baseline_pct)
            else None
        )

        resolved = LiveCall(
            timestamp=c.timestamp,
            schema_version=c.schema_version,
            ticker=c.ticker,
            catalyst_type=c.catalyst_type,
            event_date=c.event_date,
            score=c.score,
            rules_fired=c.rules_fired,
            score_components=c.score_components,
            trailing_baseline_pct=c.trailing_baseline_pct,
            outcome=outcome,
            realized_move_pct=realized,
            abs_move_pct=abs_move,
            move_ratio=move_ratio,
            resolved_at=now_iso,
            note=None,  # filled below
        )
        # Note uses the resolved call's own fields
        resolved_with_note = LiveCall(**{**resolved.__dict__, "note": _postmortem_note(resolved)})
        _write_postmortem_file(resolved_with_note)

        updated.append(resolved_with_note)
        stats["n_resolved"] += 1
        if outcome == "HIT":
            stats["n_hit"] += 1
        elif outcome == "MISS":
            stats["n_miss"] += 1
        else:
            stats["n_invalidated"] += 1

    write_calls(updated)
    log.info("live_resolve_done", **stats)
    return stats


# ---------------------------------------------------------------------------
# Status: summarize the public track record
# ---------------------------------------------------------------------------


@dataclass
class LiveStatus:
    """Snapshot of the live track record."""

    n_total: int
    n_pending: int
    n_resolved: int
    n_hits: int
    n_misses: int
    n_invalidated: int
    hit_rate_pct: float | None
    last_scan: str | None = None
    by_bucket: list[dict[str, Any]] = field(default_factory=list)


def compute_status() -> LiveStatus:
    """Read calls.csv and summarize for the README / dashboard."""
    calls = read_calls()
    n_total = len(calls)
    n_pending = sum(1 for c in calls if c.outcome == "PENDING")
    n_hits = sum(1 for c in calls if c.outcome == "HIT")
    n_misses = sum(1 for c in calls if c.outcome == "MISS")
    n_invalidated = sum(1 for c in calls if c.outcome == "INVALIDATED")
    n_resolved = n_hits + n_misses

    hit_rate = (n_hits / n_resolved * 100.0) if n_resolved > 0 else None
    last_scan = max((c.timestamp for c in calls), default=None)

    # Bucket breakdown for resolved calls
    buckets = [
        ("0-2", 0.0, 2.0),
        ("2-4", 2.0, 4.0),
        ("4-6", 4.0, 6.0),
        ("6-8", 6.0, 8.0),
        ("8-10", 8.0, 10.01),
    ]
    by_bucket: list[dict[str, Any]] = []
    for label, lo, hi in buckets:
        in_bucket = [c for c in calls if lo <= c.score < hi and c.outcome in {"HIT", "MISS"}]
        n = len(in_bucket)
        hits = sum(1 for c in in_bucket if c.outcome == "HIT")
        rate = (hits / n * 100.0) if n > 0 else None
        by_bucket.append({"label": label, "n": n, "hits": hits, "hit_rate_pct": rate})

    return LiveStatus(
        n_total=n_total,
        n_pending=n_pending,
        n_resolved=n_resolved,
        n_hits=n_hits,
        n_misses=n_misses,
        n_invalidated=n_invalidated,
        hit_rate_pct=hit_rate,
        last_scan=last_scan,
        by_bucket=by_bucket,
    )
