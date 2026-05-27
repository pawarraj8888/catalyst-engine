# Decisions Log

A running log of non-obvious choices and their reasoning. Read this before
proposing structural changes.

---

## D-001 — Sector scope: Generalist over Specialist
**Date:** 2026-05-27
**Decision:** V1 covers Healthcare (60) + Consumer (60) + Tech (80) + Industrials (50) = ~250 names.
**Alternatives:** Healthcare-only specialist (60 names, deeper biotech catalyst angle).
**Reasoning:** Target buyers are generalist L/S funds and sector pods at multi-strats. A generalist artifact opens more doors at the cost of less depth per name. Depth comes from running it live for months, not from narrowing on day one.
**Reversal cost:** Low. Can drop sectors at any time without code change — just edit `config/universe.yaml`.

## D-002 — Catalyst types: All four in V1
**Date:** 2026-05-27
**Decision:** Earnings + 8-Ks + FDA + Guidance revisions.
**Alternatives:** Earnings-only ship-fast.
**Reasoning:** Earnings alone is too commoditized — every applicant has an earnings model. The 8-K + FDA + Guidance combination is where the differentiated signal lives. Guidance revisions in particular are buried in 8-K item 7.01 / 2.02 and most retail-grade tools miss them.
**Reversal cost:** Medium. Each catalyst type has its own catalyst module and feature set. Adding/removing is contained but non-trivial.

## D-003 — Local-first, GitHub-backed
**Date:** 2026-05-27
**Decision:** Build on laptop in VS Code, push to public GitHub. No cloud deployment in V1.
**Alternatives:** Cheap VPS from day one, free-tier cloud.
**Reasoning:** Cloud adds yaks to shave (secrets management, container registries, cron infra) before the core product is proven. Laptop + GitHub Actions for CI is enough to demo. Migrating to VPS in Phase 4 is a 1-day job once the system works locally.
**Reversal cost:** Low. Dockerfile already in place; VPS deployment is config, not rewrite.

## D-004 — DuckDB over Postgres
**Date:** 2026-05-27
**Decision:** DuckDB as the warehouse engine.
**Alternatives:** Postgres, SQLite, Parquet files only.
**Reasoning:**
- Warehouse fits on laptop (~10GB V1)
- DuckDB reads Parquet natively (raw layer stays as Parquet)
- Single-file portability — the whole warehouse is one `.duckdb` file
- Columnar engine = fast aggregations for backtests
- Zero infra to manage
Postgres becomes correct if/when this goes multi-user. Not now.
**Reversal cost:** Medium. SQL is mostly portable; would need to migrate ingestion.

## D-005 — Rule-based scoring before ML
**Date:** 2026-05-27
**Decision:** YAML-configured rule-based scorer for V1. No ML until live N > 100.
**Alternatives:** XGBoost/gradient boosting on engineered features.
**Reasoning:**
- Per-catalyst-type sample sizes are small (FDA: ~80, Guidance: ~200)
- ML on small N overfits, and PMs can spot it
- Rules are inspectable — a PM can argue with `scoring.yaml`. They cannot argue with a black box.
- ML calibration *on top of* a working rule system is the right way to add it later (isotonic regression on score → realized outcome)
**Reversal cost:** Low. ML calibration is a layer on top of scoring, not a replacement.

## D-006 — Point-in-time discipline is a hard contract
**Date:** 2026-05-27
**Decision:** Every row in the warehouse has an `as_of` column. PIT correctness is enforced by tests marked `@pytest.mark.pit`. CI fails if any PIT test fails.
**Alternatives:** Vintage management by convention, manual care.
**Reasoning:** This is the single most common failure mode in research projects. The only reliable fix is mechanical enforcement. The PIT tests will be the first thing a quant PM looks at when auditing the project.
**Reversal cost:** Cannot be reversed without invalidating every backtest result.

## D-007 — uv as the package manager
**Date:** 2026-05-27
**Decision:** `uv` for dependency management and venv.
**Alternatives:** poetry, pip + venv, conda.
**Reasoning:** uv is fast, lockfile-native, drop-in compatible with `pyproject.toml`. Faster CI = faster iteration. Poetry would also be fine; the choice is taste.
**Reversal cost:** Trivial. `pyproject.toml` is portable across tools.
