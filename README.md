# Catalyst Engine

> A single-name event and catalyst tracking system for discretionary equity desks.

Tracks ~250 US equities across Healthcare, Consumer, Tech, and Industrials. Ingests
SEC filings, earnings calendars, options markets, and positioning data into a
point-in-time research warehouse. Scores upcoming catalysts (earnings, material
8-Ks, FDA events, guidance revisions) for setup quality and generates one-page
theses for high-conviction setups.

Live calls are logged publicly with timestamps. Misses are post-mortemed.

---

## Status

| Phase | Item | State |
|-------|------|-------|
| 0 | Repo scaffolding, CI, PIT contract | ✅ |
| 1 | EDGAR ingestion | ✅ |
| 1 | Earnings ingestion (Finnhub) | 🚧 next |
| 1 | Price ingestion (yfinance) | 🚧 next |
| 1 | Options snapshots (Tradier) | 🚧 next |
| 2 | Feature layer (implied move, positioning, skew) | ⏳ |
| 2 | Scoring engine | ⏳ |
| 3 | Historical backtest + walk-forward | ⏳ |
| 4 | Onepager generator + Streamlit dashboard | ⏳ |
| 4 | Intraday 8-K alerter | ⏳ |
| 5 | Live calls log (≥30 calls before applying) | ⏳ |

---

## Quick start

```bash
# 1. Clone + setup
git clone https://github.com/<you>/catalyst-engine
cd catalyst-engine

# 2. Install (uv is recommended — fast lockfile-based installs)
make dev-install

# 3. Configure
cp .env.example .env
# Edit .env: at minimum, set SEC_USER_AGENT to "Your Name your@email.com"

# 4. Initialize the universe + warehouse
catalyst universe sync

# 5. First real ingest: SEC filings for the universe (last 90d)
catalyst ingest edgar --days 90

# 6. Verify
catalyst universe show
```

---

## Project layout

```
catalyst_engine/
├── config/                  # YAML config — universe + scoring rules
├── data/                    # Warehouse (.duckdb) + raw artifacts (gitignored)
├── docs/                    # methodology.md, decisions_log.md, data_dictionary.md
├── live_log/                # Public log of calls + post-mortems
├── src/catalyst_engine/
│   ├── config.py            # Pydantic settings
│   ├── cli.py               # `catalyst` command
│   ├── db/                  # DuckDB connection + schema.sql
│   ├── data/                # Source ingestion: edgar, earnings, prices, options
│   ├── features/            # Implied move, positioning, skew
│   ├── catalysts/           # Per-catalyst setup builders
│   ├── scoring/             # YAML-driven rule scorer
│   ├── output/              # Onepager + dashboard renderers
│   ├── backtest/            # Historical replay + metrics
│   └── utils/               # logging, pit (the PIT contract lives here)
├── tests/                   # pytest, with @pytest.mark.pit being non-negotiable
└── dashboards/              # Streamlit app
```

---

## Methodology

Read [`docs/methodology.md`](docs/methodology.md) for the full design. The
short version:

1. **Universe** is point-in-time, sourced from Russell 1000 with delisted
   names included.
2. **Every row in the warehouse has an `as_of` column.** Look-ahead bias is
   prevented mechanically, not by convention. The contract is in
   `src/catalyst_engine/utils/pit.py` and enforced by `@pytest.mark.pit` tests
   that CI requires to pass.
3. **Scoring is rule-based first.** YAML-configured in `config/scoring.yaml`,
   inspectable by a PM, calibrated against historical backtest. ML calibration
   is added only after live N > 100.
4. **Live calls are public.** Every flagged setup is posted before the event
   with a timestamp. Outcomes are logged. Misses get written post-mortems.

---

## Why this project exists

Discretionary equity L/S funds and event-driven shops live on single-name
catalyst trades. A junior analyst burns ten hours a day reading filings,
checking earnings dates, watching options markets, and writing setup notes.
This system does the prep work. The judgment stays with the human.

The artifact is built to be auditable. Any past call can be re-run with
`--as-of=<date>` to verify the exact features available at that moment and
the exact rules that fired.

---

## Development

```bash
make check          # fmt + lint + type + test (everything CI runs)
make test           # pytest only
make test-pit       # PIT correctness tests (must always pass)
make cov            # coverage report → htmlcov/index.html
```

PRs without passing PIT tests are rejected.

---

## License

Proprietary — see methodology and data sources for attribution.

---

## Author

Raj Pawar — [ravadvisors.com](https://ravadvisors.com)
