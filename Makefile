.PHONY: sync check run container-check

sync:
	uv sync --frozen --all-groups

check:
	uv lock --check
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run pytest -m "not integration and not e2e"
	uv run scopecheck --help

run:
	uv run uvicorn scopecheck.app:create_app --factory --host 127.0.0.1 --port 8000

container-check:
	docker compose config
	docker build -t scopecheck:local .
	sh scripts/check-container.sh
