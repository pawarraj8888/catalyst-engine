"""SEC EDGAR ingestion.

This module is the heart of the data layer. SEC filings are the most
information-rich free data source available and the lowest-latency channel
for material corporate events.

Two endpoints we use
--------------------
1. Submissions endpoint:
       https://data.sec.gov/submissions/CIK{cik}.json
   Returns a JSON document with the company's recent filings (last ~1000 +
   older paginated). One call per company gives us the index we need to fan
   out and fetch individual filings.

2. Filing detail page:
       https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&...
   We don't use the browse-edgar HTML interface. We use the structured
   archive URL pattern instead:
       https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/
   The index.json there lists the documents in the filing.

Rate limiting
-------------
SEC enforces 10 requests/second per User-Agent. We use a token-bucket
limiter and tenacity-based retries.

Point-in-time
-------------
`as_of` for every row is set to `filed_at` (the SEC acceptance timestamp).
Filings cannot be backdated; if a filing exists at time T, it was observable
at time T. This is one of the few clean PIT sources in finance.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from catalyst_engine.config import get_settings
from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)

SEC_BASE = "https://www.sec.gov"
SEC_DATA_BASE = "https://data.sec.gov"

# Item codes worth flagging on 8-Ks (these are the material ones)
MATERIAL_8K_ITEMS = frozenset(
    {
        "1.01",  # Entry into a material definitive agreement
        "1.02",  # Termination of a material agreement
        "1.03",  # Bankruptcy
        "2.01",  # Completion of acquisition / disposition
        "2.02",  # Results of operations (earnings)
        "2.03",  # Material direct financial obligations
        "2.04",  # Triggering events accelerating obligations
        "2.05",  # Costs from exit or disposal
        "2.06",  # Material impairments
        "3.01",  # Delisting / failure to satisfy continued listing
        "3.02",  # Unregistered sales of equity
        "3.03",  # Material modification to rights of security holders
        "4.01",  # Changes in registrant's certifying accountant
        "4.02",  # Non-reliance on previously issued financials
        "5.01",  # Changes in control
        "5.02",  # Departure/election of directors and officers
        "5.03",  # Amendments to articles / bylaws
        "7.01",  # Reg FD disclosure
        "8.01",  # Other events
    }
)


@dataclass(frozen=True)
class FilingRecord:
    """A single parsed filing ready for warehouse insert."""

    accession_number: str
    cik: str
    ticker: str | None
    filing_type: str
    filed_at: datetime
    period_of_report: datetime | None
    items: list[str]
    raw_url: str
    primary_doc_url: str | None


class RateLimiter:
    """Simple token-bucket limiter to stay under SEC's 10 req/sec ceiling.

    We target 8 req/sec to leave headroom. Thread-safe-ish via asyncio.Lock
    when used in the async client.
    """

    def __init__(self, requests_per_second: float = 8.0) -> None:
        self._min_interval = 1.0 / requests_per_second
        self._last_request_time = 0.0

    def wait(self) -> None:
        """Block until the next request is allowed."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()


def _build_client(user_agent: str | None = None) -> httpx.Client:
    """Construct an HTTPX client with SEC-required headers."""
    settings = get_settings()
    ua = user_agent or settings.sec_user_agent
    if not ua or "@" not in ua:
        raise ValueError(
            "SEC requires a User-Agent containing a contact email. "
            "Set SEC_USER_AGENT in .env, e.g. 'Your Name your@email.com'."
        )
    return httpx.Client(
        timeout=30.0,
        headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
        follow_redirects=True,
    )


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def _get_json(client: httpx.Client, url: str, limiter: RateLimiter) -> dict[str, Any]:
    """GET a JSON endpoint with rate-limiting and retries."""
    limiter.wait()
    resp = client.get(url)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def fetch_submissions(cik: str, client: httpx.Client, limiter: RateLimiter) -> dict[str, Any]:
    """Fetch the submissions JSON for a CIK.

    Returns the parsed JSON. The relevant key is `filings.recent`, which has
    parallel arrays: accessionNumber, form, filingDate, primaryDocument, etc.
    """
    url = f"{SEC_DATA_BASE}/submissions/CIK{cik}.json"
    log.debug("edgar_submissions_fetch", cik=cik, url=url)
    return _get_json(client, url, limiter)


def parse_submissions_to_records(
    submissions: dict[str, Any],
    *,
    ticker: str | None,
    cik: str,
    filing_types: Iterable[str] | None = None,
    since: datetime | None = None,
) -> list[FilingRecord]:
    """Parse the submissions JSON into FilingRecord objects.

    Parameters
    ----------
    submissions : dict
        Raw output of `fetch_submissions`.
    ticker : str | None
        Resolved ticker to attach.
    cik : str
        Zero-padded 10-digit CIK.
    filing_types : Iterable[str] | None
        If provided, only keep these forms (e.g. {"8-K", "10-Q"}).
    since : datetime | None
        If provided, only keep filings on or after this date.

    Returns
    -------
    List of FilingRecord. Note: items[] is empty here — items come from the
    filing detail (index.json) which we fetch separately for 8-Ks only.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    if not recent:
        return []

    accs = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accepted_dts = recent.get("acceptanceDateTime", [])
    primary_docs = recent.get("primaryDocument", [])
    periods = recent.get("reportDate", [])
    items_list = recent.get("items", [])

    cik_int = str(int(cik))
    filter_forms = set(filing_types) if filing_types else None

    records: list[FilingRecord] = []
    for i, acc in enumerate(accs):
        form = forms[i]
        if filter_forms is not None and form not in filter_forms:
            continue

        # Prefer acceptanceDateTime (timestamped) over filingDate (date only)
        accepted_str = accepted_dts[i] if i < len(accepted_dts) else None
        if accepted_str:
            # Format: "2024-09-12T16:32:18.000Z"
            filed_at = datetime.fromisoformat(accepted_str.replace("Z", "+00:00"))
        else:
            filed_at = datetime.fromisoformat(filing_dates[i]).replace(tzinfo=timezone.utc)

        if since is not None and filed_at < since:
            continue

        # Parse items field — comma-separated string like "2.02,9.01"
        items_raw = items_list[i] if i < len(items_list) else ""
        items_parsed = (
            [item.strip() for item in items_raw.split(",") if item.strip()] if items_raw else []
        )

        period_str = periods[i] if i < len(periods) and periods[i] else None
        period_of_report = (
            datetime.fromisoformat(period_str).replace(tzinfo=timezone.utc) if period_str else None
        )

        acc_nodash = acc.replace("-", "")
        primary_doc = primary_docs[i] if i < len(primary_docs) else None
        primary_doc_url = (
            f"{SEC_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary_doc}"
            if primary_doc
            else None
        )
        raw_url = f"{SEC_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}/"

        records.append(
            FilingRecord(
                accession_number=acc,
                cik=cik,
                ticker=ticker,
                filing_type=form,
                filed_at=filed_at,
                period_of_report=period_of_report,
                items=items_parsed,
                raw_url=raw_url,
                primary_doc_url=primary_doc_url,
            )
        )

    return records


def upsert_filings(conn: duckdb.DuckDBPyConnection, records: list[FilingRecord]) -> int:
    """Insert filings into the warehouse. Idempotent on accession_number.

    Returns the number of rows actually written (new accession numbers).
    """
    if not records:
        return 0

    # Find which accession numbers already exist
    existing = conn.execute(
        "SELECT accession_number FROM filings WHERE accession_number IN ({})".format(
            ",".join("?" * len(records))
        ),
        [r.accession_number for r in records],
    ).fetchall()
    existing_set = {row[0] for row in existing}

    to_insert = [r for r in records if r.accession_number not in existing_set]
    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO filings (
            accession_number, cik, ticker, filing_type, filed_at,
            period_of_report, items, raw_url, primary_doc_url,
            body_text, as_of, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'edgar')
        """,
        [
            (
                r.accession_number,
                r.cik,
                r.ticker,
                r.filing_type,
                r.filed_at,
                r.period_of_report,
                r.items,
                r.raw_url,
                r.primary_doc_url,
                r.filed_at,  # as_of = filed_at: filings cannot be backdated
            )
            for r in to_insert
        ],
    )
    log.info("filings_upserted", new=len(to_insert), skipped=len(records) - len(to_insert))
    return len(to_insert)


def ingest_filings_for_cik(
    cik: str,
    *,
    ticker: str | None,
    conn: duckdb.DuckDBPyConnection,
    client: httpx.Client,
    limiter: RateLimiter,
    filing_types: Iterable[str] | None = None,
    since: datetime | None = None,
) -> int:
    """End-to-end ingestion for a single CIK. Returns rows written."""
    submissions = fetch_submissions(cik, client, limiter)
    records = parse_submissions_to_records(
        submissions,
        ticker=ticker,
        cik=cik,
        filing_types=filing_types,
        since=since,
    )
    return upsert_filings(conn, records)


def ingest_universe_filings(
    conn: duckdb.DuckDBPyConnection,
    *,
    universe_entries: list[tuple[str, str]],  # [(ticker, cik), ...]
    filing_types: Iterable[str] | None = ("8-K", "10-Q", "10-K", "4", "13F-HR"),
    since: datetime | None = None,
) -> dict[str, int]:
    """Bulk-ingest filings for every (ticker, cik) in the universe.

    Returns a dict {ticker: rows_written}. Tickers with no CIK are skipped.
    """
    limiter = RateLimiter(requests_per_second=8.0)
    results: dict[str, int] = {}

    with _build_client() as client:
        for ticker, cik in universe_entries:
            if not cik:
                log.warning("ingest_skip_no_cik", ticker=ticker)
                continue
            try:
                written = ingest_filings_for_cik(
                    cik,
                    ticker=ticker,
                    conn=conn,
                    client=client,
                    limiter=limiter,
                    filing_types=filing_types,
                    since=since,
                )
                results[ticker] = written
            except httpx.HTTPError as exc:
                log.error("ingest_failed", ticker=ticker, cik=cik, error=str(exc))
                results[ticker] = -1

    log.info(
        "ingest_universe_done",
        n_tickers=len(results),
        total_rows=sum(v for v in results.values() if v > 0),
    )
    return results


# Async note: the synchronous version above is correct and simple. An async
# version would shave wallclock for 250 CIKs from ~30s to ~5s. We defer the
# async rewrite until proven necessary; see decisions_log.md.
async def _async_placeholder() -> None:
    """Reserved for Phase 2 async migration if needed."""
    await asyncio.sleep(0)
