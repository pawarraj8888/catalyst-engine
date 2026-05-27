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
app.add_typer(universe_app, name="universe")
app.add_typer(ingest_app, name="ingest")
app.add_typer(backtest_app, name="backtest")
app.add_typer(features_app, name="features")

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
                f"Pulling earnings calendar: {today} → {today + _td(days=calendar_days)} "
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
            f"{start} → today, batch_size={batch_size}"
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
    console.print("[yellow]Not yet implemented — Phase 1 next step.[/yellow]")
    raise typer.Exit(2)


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


@backtest_app.command("replay")
def backtest_replay() -> None:
    """[Phase 3] Historical replay of catalyst scoring."""
    console.print("[yellow]Not yet implemented — Phase 3.[/yellow]")
    raise typer.Exit(2)


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


if __name__ == "__main__":
    app()
