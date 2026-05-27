"""Universe loader.

The YAML file `config/universe.yaml` is the source of truth for ticker
coverage. This module loads it, validates structure, and resolves CIKs from
SEC's company_tickers.json on first use.

YAML quirk
----------
PyYAML's default ``safe_load`` follows YAML 1.1, which interprets bare
strings like ``ON``, ``OFF``, ``YES``, ``NO``, ``Y``, ``N`` as booleans.
Several real tickers collide with these (e.g. ``ON`` = ON Semiconductor).
We use a custom loader that strips boolean resolution from the scanner so
all bare scalars come back as strings.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, Field

from catalyst_engine.config import get_settings
from catalyst_engine.utils.logging import get_logger

log = get_logger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class _TickerSafeLoader(yaml.SafeLoader):
    """A SafeLoader with boolean resolution stripped.

    Tickers like ``ON``, ``YES``, ``NO`` would otherwise become Python bools.
    """


# Remove the boolean resolver — bare YES/NO/ON/OFF stay as strings.
# Implicit resolvers are keyed by the first character; we drop all entries
# that mapped to bool.
def _strip_bool_resolvers(loader_cls: type[yaml.SafeLoader]) -> None:
    new_resolvers: dict[str, list[tuple[str, object]]] = {}
    for ch, mappings in loader_cls.yaml_implicit_resolvers.items():
        kept = [(tag, regex) for tag, regex in mappings if tag != "tag:yaml.org,2002:bool"]
        if kept:
            new_resolvers[ch] = kept
    loader_cls.yaml_implicit_resolvers = new_resolvers


_strip_bool_resolvers(_TickerSafeLoader)


class UniverseEntry(BaseModel):
    """A single ticker in the universe."""

    ticker: str
    sector: str
    cik: str | None = None
    company_name: str | None = None


class Universe(BaseModel):
    """The full universe as loaded from yaml + (optionally) CIK-resolved."""

    version: int = Field(default=1)
    entries: list[UniverseEntry]

    @property
    def tickers(self) -> list[str]:
        return [e.ticker for e in self.entries]

    def by_sector(self, sector: str) -> list[UniverseEntry]:
        return [e for e in self.entries if e.sector == sector]


def load_universe(path: Path | None = None) -> Universe:
    """Load the universe yaml. CIKs are NOT resolved here — call `resolve_ciks()`."""
    if path is None:
        path = get_settings().project_root / "config" / "universe.yaml"

    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_TickerSafeLoader)

    entries: list[UniverseEntry] = []
    for sector in ("healthcare", "consumer", "tech", "industrials"):
        tickers = raw.get(sector, [])
        for ticker in tickers:
            if not isinstance(ticker, str):
                # YAML 1.1 parses bare F/T/Y/N as booleans, On/Off etc. Catch it loudly.
                raise ValueError(
                    f"Ticker in sector '{sector}' parsed as {type(ticker).__name__}: "
                    f"{ticker!r}. Quote it in universe.yaml (e.g. '\"F\"')."
                )
            entries.append(UniverseEntry(ticker=ticker.upper(), sector=sector))

    log.info("universe_loaded", n_entries=len(entries), path=str(path))
    return Universe(version=raw.get("version", 1), entries=entries)


def fetch_sec_ticker_map() -> dict[str, dict[str, str]]:
    """Fetch SEC's official ticker -> CIK mapping.

    Returns a dict keyed by ticker (uppercase) with values:
        {"cik": str (10-digit zero-padded), "company_name": str}
    """
    settings = get_settings()
    headers = {"User-Agent": settings.sec_user_agent}

    log.info("sec_ticker_map_fetch_start", url=SEC_TICKERS_URL)
    with httpx.Client(timeout=30.0, headers=headers) as client:
        resp = client.get(SEC_TICKERS_URL)
        resp.raise_for_status()
        raw = resp.json()

    # SEC payload is {"0": {"cik_str": int, "ticker": str, "title": str}, ...}
    out: dict[str, dict[str, str]] = {}
    for entry in raw.values():
        ticker = entry["ticker"].upper()
        cik = f"{int(entry['cik_str']):010d}"
        out[ticker] = {"cik": cik, "company_name": entry["title"]}

    log.info("sec_ticker_map_loaded", n=len(out))
    return out


def resolve_ciks(
    universe: Universe, ticker_map: dict[str, dict[str, str]] | None = None
) -> Universe:
    """Attach CIK + company name to each universe entry.

    Tickers without a CIK match are logged but NOT dropped — they remain in
    the universe so we can surface the gap during ingestion.
    """
    if ticker_map is None:
        ticker_map = fetch_sec_ticker_map()

    resolved: list[UniverseEntry] = []
    unresolved: list[str] = []
    for entry in universe.entries:
        match = ticker_map.get(entry.ticker)
        if match is None:
            unresolved.append(entry.ticker)
            resolved.append(entry)
            continue
        resolved.append(
            UniverseEntry(
                ticker=entry.ticker,
                sector=entry.sector,
                cik=match["cik"],
                company_name=match["company_name"],
            )
        )

    if unresolved:
        log.warning("universe_ciks_unresolved", count=len(unresolved), tickers=unresolved)

    log.info(
        "universe_ciks_resolved",
        resolved=len(resolved) - len(unresolved),
        unresolved=len(unresolved),
    )
    return Universe(version=universe.version, entries=resolved)
