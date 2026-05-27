FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# Install uv (fast Python package manager)
RUN pip install uv==0.4.20

# Copy lockfile + project metadata first for layer caching
COPY pyproject.toml ./
COPY uv.lock* ./

# Install runtime deps
RUN uv pip install --system -r pyproject.toml

# Copy source last so code changes don't bust the deps layer
COPY src/ ./src/
COPY config/ ./config/

RUN pip install --no-cache-dir -e .

# Default: show CLI help
CMD ["catalyst", "--help"]
