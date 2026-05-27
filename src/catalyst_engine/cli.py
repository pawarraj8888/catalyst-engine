"""Command-line interface.

Single entry point exposed as `catalyst` (see [project.scripts] in pyproject).

Subcommands
-----------
catalyst universe sync         Resolve CIKs and persist to warehouse
catalyst ingest edgar          Pull recent SEC filings for the universe
catalyst ingest earnings       Refresh earnings calendar (Phase 1 next)
catalyst ingest prices         Refresh OHLCV (Phase 1 next)
catalyst ingest options-snapshot  Take options chain snapshot (Phase 1 next)
catalyst backtest replay       Run historical replay (Phase 3)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from catalyst_engine.config import get_settings
from catalyst_engine.data.edgar import ingest_universe_filings
from catalyst_engine.data.universe import (
    fetch_sec_ticker_map,
    load_universe,
    resolve_ciks,
)
from catalyst_engine.db import connect
from catalyst_engine.utils.logging import configure_logging, get_logger

app = typer.Typer(no_args_is_help=True, add_completion=False)
universe_app = typer.Typer(no_args_is_help=True, help="Universe management.")
ingest_app = typer.Typer(no_args_is_help=True, help="Data ingestion pipelines.")
backtest_app = typer.Typer(no_args_is_help=True, help="Backtest commands.")
features_app = typer.Typer(no_args_is_help=True, help="Feature computation.")
data_app = typer.Typer(no_args_is_help=True, help="Data quality tools.")
live_app = typer.Typer(no_args_is_help=True, help="Live calls loop.")
app.add_typer(universe_app, name="universe")
app.add_typer(ingest_app, name="ingest")
app.add_typer(backtest_app, name="backtest")
app.add_typer(features_app, name="features")
app.add_typer(data_app, name="data")
app.add_typer(live_app, name="live")

console = Console()
log = get_logger(__name__)


@app.callback()
def _root() -> None:
    """Catalyst Engine — single-name catalyst tracking."""
    configure_logging()


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------


@universe_app.command("sync")
def universe_sync() -> None:
    """Load universe.yaml, resolve CIKs via SEC, persist to warehouse."""
    universe = load_universe()
    ticker_map = fetch_sec_ticker_map()
    resolved = resolve_ciks(universe, ticker_map=ticker_map)

    today = datetime.now(UTC)
    conn = connect()
    try:
        # Wipe and reload — universe is small, simplest semantics
        conn.execute("DELETE FROM universe")
        for entry in resolved.entries:
            if entry.cik is None:
                continue
            conn.execute(
                """
                INSERT INTO universe
                (ticker, cik, company_name, sector, start_date, as_of, source)
                VALUES (?, ?, ?, ?, ?, ?, 'config')
                """,
                [entry.ticker, entry.cik, entry.company_name, entry.sector, today.date(), today],
            )
        n = conn.execute("SELECT COUNT(*) FROM universe").fetchone()
    finally:
        conn.close()

    console.print(f"[green]Universe synced: {n[0] if n else 0} entries with CIK[/green]")


@universe_app.command("show")
def universe_show() -> None:
    """Print the current universe by sector."""
    conn = connect(read_only=True)
    try:
        rows = conn.execute(
            "SELECT sector, COUNT(*) FROM universe GROUP BY sector ORDER BY sector"
        ).fetchall()
    finally:
        conn.close()

    table = Table(title="Universe")
    table.add_column("Sector")
    table.add_column("Count", justify="right")
    for sector, n in rows:
        table.add_row(sector, str(n))
    console.print(table)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@ingest_app.command("edgar")
def ingest_edgar(
    days: int = typer.Option(90, help="Lookback window in days"),
    forms: str = typer.Option(
        "8-K,10-Q,10-K,4,13F-HR",
        help="Comma-separated filing forms to ingest",
    ),
) -> None:
    """Pull recent SEC filings for the universe."""
    settings = get_settings()
    if "@" not in settings.sec_user_agent:
        console.print("[red]SEC_USER_AGENT must include an email. Set it in .env first.[/red]")
        raise typer.Exit(1)

    conn = connect()
    try:
        entries = conn.execute("SELECT ticker, cik FROM universe WHERE cik IS NOT NULL").fetchall()
        if not entries:
            console.print(
                "[yellow]No universe rows with CIK. Run `catalyst universe sync` first.[/yellow]"
            )
            raise typer.Exit(1)

        since = datetime.now(UTC) - timedelta(days=days)
        filing_types = tuple(f.strip() for f in forms.split(","))

        console.print(
            f"Ingesting filings for {len(entries)} tickers, lookback={days}d, forms={filing_types}"
        )
        results = ingest_universe_filings(
            conn,
            universe_entries=list(entries),
            filing_types=filing_types,
            since=since,
        )
    finally:
        conn.close()

    successes = sum(1 for v in results.values() if v >= 0)
    total_rows = sum(v for v in results.values() if v > 0)
    failures = sum(1 for v in results.values() if v < 0)
    console.print(
        f"[green]Done. Tickers: {successes} success, {failures} failed. "
        f"Total new rows: {total_rows}[/green]"
    )


@ingest_app.command("earnings")
def ingest_earnings(
    calendar_days: int = typer.Option(
        90, help="Forward calendar window in days (upcoming earnings)"
    ),
    history_quarters: int = typer.Option(
        20, help="Historical surprise depth per ticker (max ~20 on free tier)"
    ),
    skip_calendar: bool = typer.Option(False, help="Skip calendar refresh"),
    skip_history: bool = typer.Option(False, help="Skip surprise history pull"),
) -> None:
    """Refresh earnings calendar (forward) and surprise history (backward).

    Uses Finnhub. Requires FINNHUB_API_KEY in .env.
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    from catalyst_engine.data.earnings import (
        ingest_calendar_window,
        ingest_surprise_history,
    )

    settings = get_settings()
    if not settings.finnhub_api_key:
        console.print(
            "[red]FINNHUB_API_KEY is empty. Get a free key at "
            "https://finnhub.io/register and set it in .env.[/red]"
        )
        raise typer.Exit(1)

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT ticker FROM universe WHERE cik IS NOT NULL ORDER BY ticker"
        ).fetchall()
        if not rows:
            console.print("[yellow]Universe is empty. Run `catalyst universe sync` first.[/yellow]")
            raise typer.Exit(1)
        tickers = [r[0] for r in rows]

        total_new_calendar = 0
        if not skip_calendar:
            today = _date.today()
            console.print(
                f"Pulling earnings calendar: {today} -> {today + _td(days=calendar_days)} "
                f"(universe filter: {len(tickers)} tickers)"
            )
            total_new_calendar = ingest_calendar_window(
                conn,
                start=today,
                end=today + _td(days=calendar_days),
                universe_tickers=set(tickers),
            )
            console.print(f"[green]Calendar: {total_new_calendar} new rows[/green]")

        total_new_history = 0
        if not skip_history:
            console.print(
                f"Pulling surprise history: {len(tickers)} tickers x ~{history_quarters} quarters"
            )
            results = ingest_surprise_history(
                conn, tickers=tickers, limit_per_ticker=history_quarters
            )
            successes = sum(1 for v in results.values() if v >= 0)
            failures = sum(1 for v in results.values() if v < 0)
            total_new_history = sum(v for v in results.values() if v > 0)
            console.print(
                f"[green]History: {successes} success, {failures} failed, "
                f"{total_new_history} new rows[/green]"
            )

        total_in_db = conn.execute("SELECT COUNT(*) FROM earnings_events").fetchone()
        console.print(
            f"[bold]Earnings events in warehouse: {total_in_db[0] if total_in_db else 0}[/bold]"
        )
    finally:
        conn.close()


@ingest_app.command("prices")
def ingest_prices(
    years: int = typer.Option(5, help="Lookback in years"),
    batch_size: int = typer.Option(50, help="Tickers per yfinance bulk call"),
) -> None:
    """Refresh daily OHLCV via yfinance for the entire universe."""
    from datetime import date as _date
    from datetime import timedelta as _td

    from catalyst_engine.data.prices import ingest_prices_for_universe

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT ticker FROM universe WHERE cik IS NOT NULL ORDER BY ticker"
        ).fetchall()
        if not rows:
            console.print("[yellow]Universe empty. Run `catalyst universe sync` first.[/yellow]")
            raise typer.Exit(1)
        tickers = [r[0] for r in rows]

        start = _date.today() - _td(days=365 * years)
        console.print(
            f"Pulling daily OHLCV: {len(tickers)} tickers, "
            f"{start} -> today, batch_size={batch_size}"
        )
        n = ingest_prices_for_universe(conn, tickers=tickers, start=start, batch_size=batch_size)

        total = conn.execute("SELECT COUNT(*) FROM prices").fetchone()
        console.print(f"[green]Prices: {n} new rows[/green]")
        console.print(f"[bold]Total price rows in warehouse: {total[0] if total else 0}[/bold]")
    finally:
        conn.close()


@ingest_app.command("earnings-backfill")
def ingest_earnings_backfill() -> None:
    """Deepen earnings history from yfinance (~8-12 quarters per ticker).

    Use this after `catalyst ingest earnings` to supplement Finnhub's
    free-tier 4-quarter cap. Writes to the same `earnings_events` table.
    """
    from catalyst_engine.data.prices import ingest_yf_earnings_backfill

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT ticker FROM universe WHERE cik IS NOT NULL ORDER BY ticker"
        ).fetchall()
        if not rows:
            console.print("[yellow]Universe empty. Run `catalyst universe sync` first.[/yellow]")
            raise typer.Exit(1)
        tickers = [r[0] for r in rows]

        console.print(f"Backfilling earnings history from yfinance for {len(tickers)} tickers...")
        console.print("[dim]This may take 3-5 minutes — yfinance scrapes Yahoo.[/dim]")
        results = ingest_yf_earnings_backfill(conn, tickers=tickers)

        successes = sum(1 for v in results.values() if v >= 0)
        failures = sum(1 for v in results.values() if v < 0)
        total_new = sum(v for v in results.values() if v > 0)

        total = conn.execute("SELECT COUNT(*) FROM earnings_events").fetchone()
        console.print(
            f"[green]Backfill: {successes} success, {failures} failed, "
            f"{total_new} new rows[/green]"
        )
        console.print(
            f"[bold]Total earnings events in warehouse: {total[0] if total else 0}[/bold]"
        )
    finally:
        conn.close()


@ingest_app.command("options-snapshot")
def ingest_options_snapshot() -> None:
    """[Phase 1 next] Snapshot options chains via Tradier."""
    console.print("[yellow]Not yet implemented - Phase 1 next step.[/yellow]")
    raise typer.Exit(2)


@ingest_app.command("short-interest")
def ingest_short_interest() -> None:
    """Fetch FINRA short interest snapshots for the entire universe."""
    from catalyst_engine.data.short_interest import ingest_universe
    from catalyst_engine.data.universe import load_universe

    universe = load_universe()
    tickers = universe.tickers

    conn = connect()
    try:
        console.print(f"Fetching short interest for {len(tickers)} tickers...")
        n = ingest_universe(conn, tickers)
        console.print(f"[green]{n} new short_interest rows written[/green]")
    finally:
        conn.close()


@ingest_app.command("insider-bulk")
def ingest_insider_bulk(
    start: str = typer.Option("2021Q1", help="First quarter, e.g. 2021Q1"),
    end: str = typer.Option("2026Q1", help="Last quarter (inclusive)"),
) -> None:
    """Download SEC bulk insider-transaction ZIPs and ingest into insider_transactions.

    SEC publishes quarterly ZIPs containing every Form 3/4/5 filing's
    structured transactions. This is the gold-standard insider data source.
    """
    import re

    from catalyst_engine.data.insider_bulk import ingest_quarters
    from catalyst_engine.data.universe import load_universe

    def _parse(qstr: str) -> tuple[int, int]:
        m = re.fullmatch(r"(\d{4})Q([1-4])", qstr.upper().strip())
        if not m:
            raise typer.BadParameter(f"Expected format YYYYQN, got {qstr!r}")
        return int(m.group(1)), int(m.group(2))

    sy, sq = _parse(start)
    ey, eq = _parse(end)
    universe = load_universe()
    tickers = universe.tickers

    conn = connect()
    try:
        console.print(f"Ingesting insider bulk: {start} -> {end}, {len(tickers)} universe tickers")
        stats = ingest_quarters(conn, tickers, start_year=sy, start_q=sq, end_year=ey, end_q=eq)
        total = sum(v for v in stats.values() if v > 0)
        failed = [k for k, v in stats.items() if v < 0]
        console.print(f"[green]Inserted {total} insider transactions[/green]")
        if failed:
            console.print(f"[yellow]Quarters with errors: {failed}[/yellow]")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


@backtest_app.command("replay")
def backtest_replay(
    start: str = typer.Option(None, help="ISO start date (e.g. 2023-01-01)"),
    end: str = typer.Option(None, help="ISO end date"),
    catalyst_type: str = typer.Option("earnings", help="Which catalyst family"),
) -> None:
    """Run a historical backtest: score every past event and report hit rates."""
    from datetime import date as _date

    from catalyst_engine.backtest.metrics import compute_summary
    from catalyst_engine.backtest.replay import replay
    from catalyst_engine.scoring.scorer import load_scoring_config

    config = load_scoring_config()
    start_date = _date.fromisoformat(start) if start else None
    end_date = _date.fromisoformat(end) if end else None

    conn = connect()
    try:
        console.print(
            f"[bold]Replaying {catalyst_type} setups[/bold]"
            f"{' from ' + start if start else ''}"
            f"{' to ' + end if end else ''}"
        )
        run_id, n_written = replay(
            conn,
            config=config,
            catalyst_type=catalyst_type,
            start=start_date,
            end=end_date,
        )
        console.print(f"[dim]run_id={run_id}, {n_written} rows scored[/dim]\n")

        summary = compute_summary(conn, run_id=str(run_id))

        console.print(
            f"[bold]Overall:[/bold] {summary.n_evaluable} evaluable setups, "
            f"hit rate = {summary.overall_hit_rate:.1%}\n"
        )

        table = Table(title="Hit rate by score bucket (95% bootstrap CI)")
        table.add_column("Bucket", justify="left")
        table.add_column("N", justify="right")
        table.add_column("Hits", justify="right")
        table.add_column("Rate", justify="right")
        table.add_column("CI low", justify="right")
        table.add_column("CI high", justify="right")
        for b in summary.buckets:
            table.add_row(
                b.bucket_label,
                str(b.n_setups),
                str(b.n_hits),
                f"{b.hit_rate:.1%}",
                f"{b.ci_low_95:.1%}",
                f"{b.ci_high_95:.1%}",
            )
        console.print(table)
        console.print(
            "\n[dim]Hit = abs_move_1d > trailing_median_8q. "
            "V0 rules use only realized-moves history. "
            "Real lift expected once positioning/skew features land in Phase 2.[/dim]"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


@features_app.command("realized-moves")
def features_realized_moves(
    window: int = typer.Option(8, help="Rolling window size for trailing median"),
) -> None:
    """Compute realized moves for every historical earnings event.

    Joins earnings_events x prices into the realized_moves table. This is
    the label set the backtest reads from.
    """
    from catalyst_engine.features.realized_moves import compute_universe_moves

    conn = connect()
    try:
        console.print(f"Computing realized moves (window={window})...")
        n = compute_universe_moves(conn, only_with_actuals=True, window=window)

        total = conn.execute("SELECT COUNT(*) FROM realized_moves").fetchone()
        with_1d = conn.execute(
            "SELECT COUNT(*) FROM realized_moves WHERE realized_move_1d IS NOT NULL"
        ).fetchone()
        with_median = conn.execute(
            "SELECT COUNT(*) FROM realized_moves WHERE trailing_median_8q IS NOT NULL"
        ).fetchone()

        console.print(f"[green]{n} new rows written[/green]")
        console.print(
            f"[bold]realized_moves: {total[0] if total else 0} total, "
            f"{with_1d[0] if with_1d else 0} with 1d move, "
            f"{with_median[0] if with_median else 0} with trailing median[/bold]"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# data — quality audits and repairs
# ---------------------------------------------------------------------------


@data_app.command("audit-earnings")
def data_audit_earnings(
    threshold: int = typer.Option(50, help="Concentration threshold to flag a date as suspicious"),
) -> None:
    """Inspect earnings_events for fake fiscal-period-end dates."""
    from catalyst_engine.data.earnings_quality import audit_earnings_dates

    conn = connect(read_only=True)
    try:
        result = audit_earnings_dates(conn, concentration_threshold=threshold)
    finally:
        conn.close()

    console.print(f"[bold]Audit:[/bold] {result.total_events} historical events")
    if not result.suspicious:
        console.print("[green]No suspicious quarter-end concentrations. Clean.[/green]")
        return

    console.print(
        f"[yellow]{result.n_suspicious_events} events across "
        f"{result.n_suspicious_dates} suspicious quarter-end dates:[/yellow]\n"
    )
    table = Table()
    table.add_column("event_date")
    table.add_column("n_tickers", justify="right")
    for s in result.suspicious:
        table.add_row(str(s.event_date), str(s.n_tickers))
    console.print(table)
    console.print("\nRun [bold]catalyst data clean-earnings[/bold] to drop these rows.")


@data_app.command("clean-earnings")
def data_clean_earnings(
    threshold: int = typer.Option(50, help="Concentration threshold"),
    apply: bool = typer.Option(
        False, "--apply", help="Actually delete. Without this flag, runs dry."
    ),
) -> None:
    """Delete suspicious earnings rows and their downstream realized_moves."""
    from catalyst_engine.data.earnings_quality import clean_fake_earnings_dates

    conn = connect()
    try:
        n_earn, n_moves = clean_fake_earnings_dates(
            conn, concentration_threshold=threshold, dry_run=not apply
        )
    finally:
        conn.close()

    verb = "[red]Deleted[/red]" if apply else "[yellow]Would delete[/yellow]"
    console.print(f"{verb}: {n_earn} earnings_events rows, {n_moves} realized_moves rows")
    if not apply:
        console.print("\nRe-run with [bold]--apply[/bold] to perform the deletes.")


@data_app.command("rebuild-earnings-from-edgar")
def data_rebuild_from_edgar(
    apply: bool = typer.Option(
        False, "--apply", help="Actually insert. Without this flag, runs dry."
    ),
) -> None:
    """Use 8-K item 2.02 filings as the source for announcement dates."""
    from catalyst_engine.data.earnings_from_edgar import rebuild_from_edgar

    conn = connect()
    try:
        stats = rebuild_from_edgar(conn, dry_run=not apply)
    finally:
        conn.close()

    console.print("[bold]EDGAR rebuild stats[/bold]")
    for k, v in stats.items():
        console.print(f"  {k}: {v}")
    if not apply:
        console.print("\nRe-run with [bold]--apply[/bold] to insert announcement-dated rows.")


@data_app.command("clear-derived")
def data_clear_derived(
    apply: bool = typer.Option(
        False, "--apply", help="Actually delete. Without this flag, runs dry."
    ),
) -> None:
    """Wipe derived tables (realized_moves, scored_setups).

    Raw data (universe, filings, earnings_events, prices) is preserved.
    Use this when you've changed feature logic or fixed data and want a
    clean recompute.
    """
    conn = connect()
    try:
        n_rm = conn.execute("SELECT COUNT(*) FROM realized_moves").fetchone()[0]
        n_ss = conn.execute("SELECT COUNT(*) FROM scored_setups").fetchone()[0]
        if apply:
            conn.execute("DELETE FROM scored_setups")
            conn.execute("DELETE FROM realized_moves")
            console.print(
                f"[red]Deleted[/red]: {n_rm} realized_moves rows, {n_ss} scored_setups rows"
            )
        else:
            console.print(
                f"[yellow]Would delete[/yellow]: {n_rm} realized_moves, {n_ss} scored_setups"
            )
            console.print("\nRe-run with --apply to actually delete.")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Live calls loop
# ---------------------------------------------------------------------------


@live_app.command("scan")
def live_scan(
    horizon: int = typer.Option(14, help="Look ahead this many days for upcoming events."),
    min_score: float = typer.Option(
        -999.0, help="Only log calls with score >= this. Default: log everything."
    ),
) -> None:
    """Score upcoming catalysts and append new calls to live_log/calls.csv.

    Idempotent on (ticker, event_date) - re-running won't duplicate rows.
    Typically run once per weekday morning via GitHub Actions cron.
    """
    from catalyst_engine.live import scan_and_log
    from catalyst_engine.scoring.scorer import load_scoring_config

    config = load_scoring_config()
    conn = connect()
    try:
        ms = None if min_score <= -999.0 else min_score
        n_new, n_skipped = scan_and_log(
            conn,
            config=config,
            horizon_days=horizon,
            min_score=ms,
        )
        console.print(
            f"[green]Logged {n_new} new calls[/green] " f"(skipped {n_skipped} already-logged)"
        )
    finally:
        conn.close()


@live_app.command("resolve")
def live_resolve() -> None:
    """Fill in realized outcomes for PENDING calls whose event_date has passed.

    Computes the realized 1-day move, classifies HIT/MISS/INVALIDATED, and
    writes a post-mortem markdown file for non-HIT outcomes.
    Typically run once per weekday evening via GitHub Actions cron.
    """
    from catalyst_engine.live import resolve_pending_calls

    conn = connect()
    try:
        stats = resolve_pending_calls(conn)
        console.print(
            f"[green]Resolved {stats['n_resolved']} calls[/green]: "
            f"{stats['n_hit']} HIT, {stats['n_miss']} MISS, "
            f"{stats['n_invalidated']} INVALIDATED. "
            f"{stats['n_pending_remaining']} still pending."
        )
    finally:
        conn.close()


@live_app.command("status")
def live_status() -> None:
    """Print a snapshot of the live track record."""
    from rich.table import Table

    from catalyst_engine.live import compute_status

    status = compute_status()
    console.print(f"[bold]Live calls log:[/bold] {status.n_total} total")
    console.print(f"  Pending:     {status.n_pending}")
    console.print(f"  Resolved:    {status.n_resolved}")
    console.print(f"    Hits:      {status.n_hits}")
    console.print(f"    Misses:    {status.n_misses}")
    console.print(f"  Invalidated: {status.n_invalidated}")
    if status.hit_rate_pct is not None:
        console.print(f"  [bold]Hit rate:    {status.hit_rate_pct:.1f}%[/bold]")
        if status.n_resolved < 30:
            console.print(
                f"  [yellow]Note: weights not trusted until N>=30 "
                f"(currently {status.n_resolved})[/yellow]"
            )
    if status.last_scan:
        console.print(f"  Last scan:   {status.last_scan}")

    if any(b["n"] > 0 for b in status.by_bucket):
        table = Table(title="\nResolved calls by score bucket")
        table.add_column("Bucket")
        table.add_column("N", justify="right")
        table.add_column("Hits", justify="right")
        table.add_column("Hit rate", justify="right")
        for b in status.by_bucket:
            if b["n"] == 0:
                continue
            rate = f"{b['hit_rate_pct']:.1f}%" if b["hit_rate_pct"] is not None else "n/a"
            table.add_row(b["label"], str(b["n"]), str(b["hits"]), rate)
        console.print(table)


if __name__ == "__main__":
    app()
