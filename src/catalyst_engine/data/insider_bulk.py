"""SEC Insider Transactions bulk dataset ingestion.

Source
------
https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets

The SEC publishes a quarterly ZIP containing every Form 3/4/5 filing's
transactions, pre-parsed from the XML into tab-separated value files.
This is the gold-standard source for insider sentiment research; it's
free, no auth, no scraping, and updated quarterly.

URL pattern:
    https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{YYYY}q{Q}_form345.zip

Each ZIP contains these TSVs (the schema is documented in
https://www.sec.gov/files/insider_transactions_readme.pdf):

- SUBMISSION.tsv       — one row per filing: accession_no, filing_date,
                          period_of_report, form_type, issuer CIK/name/symbol
- REPORTINGOWNER.tsv   — one row per (filing, insider): name, role flags
                          (is_director, is_officer, is_ten_percent_owner),
                          officer_title
- NONDERIV_TRANS.tsv   — open-market and other non-derivative transactions
                          (this is what we care about for buys/sells)
- DERIV_TRANS.tsv      — option exercises, grants (less important for V1)
- FOOTNOTES.tsv        — free-text annotations referenced by transaction
                          rows (10b5-1 disclosures live here)
- OWNER_SIGNATURE.tsv  — filing signatures (not used)

Key transaction codes (from `NONDERIV_TRANS.TRANS_CODE`):
- P  Open-market purchase     <-- biggest signal
- S  Open-market sale         <-- second biggest signal
- A  Grant/award              <-- noise (compensation)
- M  Option exercise          <-- noise (planned)
- F  Tax-withholding          <-- noise (automatic)
- D  Disposition (non-cash)   <-- noise
- G  Gift                     <-- noise
- W  Will/inheritance         <-- noise

10b5-1 detection
-----------------
The bulk dataset doesn't have a structured "is_10b5_1" column. The
indication is text-based: footnotes referencing "10b5-1" or "Rule 10b5-1
plan". We extract this by joining FOOTNOTES on the transaction's
footnote_id columns.

In V0 we set is_10b5_1=True when ANY footnote attached to the
transaction contains "10b5-1" (case insensitive). False positives are
rare; false negatives possible if the filing is sloppy.

Scope
-----
We filter to:
- Form 4 only (current changes; Form 3 is initial holdings, Form 5 is
  annual catch-up — both lower signal)
- Issuer in our universe (by ticker symbol)
- Non-derivative transactions only (NONDERIV_TRANS.tsv)
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from catalyst_engine.config import get_settings
from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    import duckdb

log = get_logger(__name__)


BULK_URL_TEMPLATE = (
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"
    "{year}q{quarter}_form345.zip"
)

# Transaction codes that carry directional information about insider sentiment.
# Other codes (A, M, F, D, G, W) are excluded from feature computation.
SIGNAL_CODES: frozenset[str] = frozenset({"P", "S"})


@dataclass(frozen=True)
class InsiderTransactionRow:
    """One non-derivative transaction destined for insider_transactions."""

    accession_number: str
    ticker: str
    filer_name: str | None
    filer_title: str | None
    transaction_date: date
    transaction_code: str
    shares: int
    price: float | None
    value_usd: float | None
    is_10b5_1: bool


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _bulk_url(year: int, quarter: int) -> str:
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    return BULK_URL_TEMPLATE.format(year=year, quarter=quarter)


def download_quarter_zip(
    year: int,
    quarter: int,
    *,
    cache_dir: Path | None = None,
    user_agent: str | None = None,
) -> Path:
    """Download a quarterly Form 3/4/5 ZIP to local cache and return its path.

    If the file already exists in cache, it's reused (the SEC quarterly
    files don't change after release except for the most recent quarter,
    which is refreshed daily). To force a refresh, delete the cached file.
    """
    if cache_dir is None:
        cache_dir = get_settings().project_root / "data" / "raw" / "insider_bulk"
    cache_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{year}q{quarter}_form345.zip"
    local_path = cache_dir / filename

    if local_path.exists() and local_path.stat().st_size > 0:
        log.debug("insider_bulk_cache_hit", path=str(local_path))
        return local_path

    ua = user_agent or get_settings().sec_user_agent
    if not ua or "@" not in ua:
        raise RuntimeError(
            "SEC_USER_AGENT must be set and include an email address. "
            "See https://www.sec.gov/os/accessing-edgar-data"
        )

    url = _bulk_url(year, quarter)
    log.info("insider_bulk_downloading", url=url)
    with httpx.Client(headers={"User-Agent": ua}, follow_redirects=True) as client:
        resp = client.get(url, timeout=120.0)
        resp.raise_for_status()

    local_path.write_bytes(resp.content)
    log.info(
        "insider_bulk_downloaded",
        path=str(local_path),
        bytes=local_path.stat().st_size,
    )
    return local_path


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _read_tsv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    """Read a TSV from the SEC ZIP into a list of dicts."""
    candidates = [n for n in zf.namelist() if n.upper().endswith(name.upper())]
    if not candidates:
        raise FileNotFoundError(f"TSV {name} not present in archive members={zf.namelist()}")
    with zf.open(candidates[0]) as f:
        text = io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text, delimiter="\t")
        return [dict(row) for row in reader]


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(s: str | None) -> int | None:
    f = _parse_float(s)
    return int(f) if f is not None else None


def parse_quarter_zip(
    zip_path: Path,
    *,
    universe_tickers: set[str],
) -> list[InsiderTransactionRow]:
    """Parse a single quarterly ZIP into InsiderTransactionRow list.

    Filters to:
    - Form 4 filings
    - Issuers whose ISSUERTRADINGSYMBOL is in universe_tickers
    - Non-derivative transactions only

    Joins SUBMISSION (issuer info), REPORTINGOWNER (filer role/title),
    NONDERIV_TRANS (the transaction), and FOOTNOTES (for 10b5-1 detection).
    """
    universe_upper = {t.upper() for t in universe_tickers}

    with zipfile.ZipFile(zip_path) as zf:
        submissions = _read_tsv(zf, "SUBMISSION.tsv")
        reporters = _read_tsv(zf, "REPORTINGOWNER.tsv")
        transactions = _read_tsv(zf, "NONDERIV_TRANS.tsv")
        footnotes = _read_tsv(zf, "FOOTNOTES.tsv")

    # Index: accession_no -> submission row
    sub_by_acc: dict[str, dict[str, str]] = {}
    for row in submissions:
        if (row.get("DOCUMENT_TYPE") or "").strip() != "4":
            continue
        sym = (row.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        if sym not in universe_upper:
            continue
        acc = (row.get("ACCESSION_NUMBER") or "").strip()
        if not acc:
            continue
        sub_by_acc[acc] = row

    # Index: accession_no -> first reporter (primary insider on the filing)
    reporter_by_acc: dict[str, dict[str, str]] = {}
    for row in reporters:
        acc = (row.get("ACCESSION_NUMBER") or "").strip()
        if acc and acc not in reporter_by_acc:
            reporter_by_acc[acc] = row

    # Index: (accession_no, footnote_id) -> text. For 10b5-1 detection we
    # look at the footnotes referenced by each transaction.
    footnote_text_by_acc: dict[str, str] = {}
    for row in footnotes:
        acc = (row.get("ACCESSION_NUMBER") or "").strip()
        text = (row.get("FOOTNOTE_TXT") or "").strip()
        if not acc or not text:
            continue
        # Concatenate all footnotes per filing — 10b5-1 mention in ANY
        # footnote is a strong signal the filing was a planned trade.
        prior = footnote_text_by_acc.get(acc, "")
        footnote_text_by_acc[acc] = (prior + " " + text).strip()

    out: list[InsiderTransactionRow] = []
    for trans in transactions:
        acc = (trans.get("ACCESSION_NUMBER") or "").strip()
        if acc not in sub_by_acc:
            continue
        sub = sub_by_acc[acc]
        reporter = reporter_by_acc.get(acc, {})

        ticker = (sub.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        trans_date = _parse_date(trans.get("TRANS_DATE"))
        trans_code = (trans.get("TRANS_CODE") or "").strip().upper()
        shares = _parse_int(trans.get("TRANS_SHARES"))
        price = _parse_float(trans.get("TRANS_PRICEPERSHARE"))

        if trans_date is None or not trans_code or shares is None:
            continue

        value = (price * shares) if (price is not None and shares is not None) else None
        # A sale's "value_usd" we leave positive; sign convention is carried
        # by transaction_code (P=buy, S=sell). Downstream features signed.

        # 10b5-1 detection: search the filing's footnotes
        footnote_blob = footnote_text_by_acc.get(acc, "").lower()
        is_10b5_1 = "10b5-1" in footnote_blob or "rule 10b5" in footnote_blob

        filer_name = (reporter.get("RPTOWNERNAME") or "").strip() or None
        filer_title = (reporter.get("RPTOWNER_TITLE") or "").strip() or None

        out.append(
            InsiderTransactionRow(
                accession_number=acc,
                ticker=ticker,
                filer_name=filer_name,
                filer_title=filer_title,
                transaction_date=trans_date,
                transaction_code=trans_code,
                shares=shares,
                price=price,
                value_usd=value,
                is_10b5_1=is_10b5_1,
            )
        )

    log.info(
        "insider_bulk_parsed",
        path=str(zip_path),
        n_rows=len(out),
        n_submissions=len(sub_by_acc),
    )
    return out


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def upsert_insider_transactions(
    conn: duckdb.DuckDBPyConnection, rows: list[InsiderTransactionRow]
) -> int:
    """Insert insider transactions; idempotent on the table's PK.

    Schema PK is (accession_number, ticker, transaction_date,
    transaction_code, shares). We dedupe in-batch then check existing.
    """
    if not rows:
        return 0

    now_naive = datetime.now(UTC).replace(tzinfo=None)

    seen: set[tuple[str, str, date, str, int]] = set()
    deduped: list[InsiderTransactionRow] = []
    for r in rows:
        key = (
            r.accession_number,
            r.ticker,
            r.transaction_date,
            r.transaction_code,
            r.shares,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    placeholders = ",".join("(?, ?, ?, ?, ?)" for _ in deduped)
    existing_rows = conn.execute(
        f"""
        SELECT accession_number, ticker, transaction_date, transaction_code, shares
        FROM insider_transactions
        WHERE (accession_number, ticker, transaction_date, transaction_code, shares)
              IN ({placeholders})
        """,
        [
            v
            for r in deduped
            for v in (
                r.accession_number,
                r.ticker,
                r.transaction_date,
                r.transaction_code,
                r.shares,
            )
        ],
    ).fetchall()
    existing = {(r[0], r[1], r[2], r[3], r[4]) for r in existing_rows}

    to_insert = [
        r
        for r in deduped
        if (
            r.accession_number,
            r.ticker,
            r.transaction_date,
            r.transaction_code,
            r.shares,
        )
        not in existing
    ]
    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO insider_transactions
        (accession_number, ticker, filer_name, filer_title, transaction_date,
         transaction_code, shares, price, value_usd, is_10b5_1, as_of, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_bulk')
        """,
        [
            (
                r.accession_number,
                r.ticker,
                r.filer_name,
                r.filer_title,
                r.transaction_date,
                r.transaction_code,
                r.shares,
                r.price,
                r.value_usd,
                r.is_10b5_1,
                now_naive,
            )
            for r in to_insert
        ],
    )
    log.info(
        "insider_transactions_upserted",
        new=len(to_insert),
        skipped=len(deduped) - len(to_insert),
    )
    return len(to_insert)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def quarters_in_range(
    start_year: int, start_q: int, end_year: int, end_q: int
) -> list[tuple[int, int]]:
    """Inclusive list of (year, quarter) pairs."""
    out: list[tuple[int, int]] = []
    y, q = start_year, start_q
    while (y, q) <= (end_year, end_q):
        out.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1
    return out


def ingest_quarters(
    conn: duckdb.DuckDBPyConnection,
    universe_tickers: list[str],
    *,
    start_year: int,
    start_q: int,
    end_year: int,
    end_q: int,
) -> dict[str, int]:
    """End-to-end ingest of a range of quarters. Returns per-quarter stats."""
    universe_set = {t.upper() for t in universe_tickers}
    stats: dict[str, int] = {}
    for year, quarter in quarters_in_range(start_year, start_q, end_year, end_q):
        key = f"{year}Q{quarter}"
        try:
            zip_path = download_quarter_zip(year, quarter)
            rows = parse_quarter_zip(zip_path, universe_tickers=universe_set)
            n = upsert_insider_transactions(conn, rows)
            stats[key] = n
            log.info("insider_bulk_quarter_done", quarter=key, rows_written=n)
        except Exception as exc:
            log.error("insider_bulk_quarter_error", quarter=key, error=str(exc))
            stats[key] = -1
    return stats
