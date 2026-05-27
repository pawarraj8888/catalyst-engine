.PHONY: help install dev-install fmt lint type test test-pit cov clean run-edgar run-earnings run-prices run-options snapshot backtest dashboard

help:
	@echo "Catalyst Engine — common commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install        Install runtime deps (uv)"
	@echo "  make dev-install    Install runtime + dev deps + pre-commit hooks"
	@echo ""
	@echo "Quality:"
	@echo "  make fmt            Format with black + ruff fix"
	@echo "  make lint           Lint with ruff (no fix)"
	@echo "  make type           Type-check with mypy"
	@echo "  make test           Run all tests"
	@echo "  make test-pit       Run only point-in-time correctness tests"
	@echo "  make cov            Run tests with coverage report"
	@echo "  make check          Run fmt + lint + type + test (everything)"
	@echo ""
	@echo "Data pipelines:"
	@echo "  make run-edgar      Ingest recent SEC filings"
	@echo "  make run-earnings   Refresh earnings calendar + history"
	@echo "  make run-prices     Refresh daily OHLCV"
	@echo "  make snapshot       Take options chain snapshot (cron at 15:55 ET)"
	@echo ""
	@echo "Research:"
	@echo "  make backtest       Run historical replay"
	@echo "  make dashboard      Launch Streamlit dashboard"

install:
	uv sync

dev-install:
	uv sync --extra dev --extra dashboard
	uv run pre-commit install

fmt:
	uv run ruff check --fix src tests
	uv run black src tests

lint:
	uv run ruff check src tests

type:
	uv run mypy src

test:
	uv run pytest

test-pit:
	uv run pytest -m pit -v

cov:
	uv run pytest --cov-report=html
	@echo "Open htmlcov/index.html"

check: fmt lint type test
	@echo "All checks passed."

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +

# --- Data pipelines (wired in Phase 1) ---
run-edgar:
	uv run catalyst ingest edgar

run-earnings:
	uv run catalyst ingest earnings

run-prices:
	uv run catalyst ingest prices

snapshot:
	uv run catalyst ingest options-snapshot

backtest:
	uv run catalyst backtest replay

dashboard:
	uv run streamlit run dashboards/streamlit_app.py
