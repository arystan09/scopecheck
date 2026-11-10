FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
RUN groupadd --gid 10001 scopecheck \
    && useradd --uid 10001 --gid 10001 --no-create-home scopecheck
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY datasets ./datasets
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SCOPECHECK_MODE=fake
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
CMD ["uvicorn", "scopecheck.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
