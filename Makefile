# Every target runs through uv, which syncs .venv from uv.lock first, so nobody manages a
# virtual environment by hand. CI runs the same targets with UV_LOCKED=1.

.PHONY: sync fmt lint typecheck test cov check

sync:
	uv sync

fmt:
	uv run ruff check --fix
	uv run ruff format

lint:
	uv run ruff check
	uv run ruff format --check

# stubtest checks typings/brotli.pyi against the installed brotli, so an upgrade can't
# leave the stub stale.
typecheck:
	uv run mypy
	uv run python -m mypy.stubtest brotli --mypy-config-file pyproject.toml \
		--allowlist typings/stubtest-allowlist.txt

# PYTEST_ARGS passes options through, e.g. make test PYTEST_ARGS="-k frontier -x".
test:
	uv run coverage erase
	uv run coverage run -m pytest $(PYTEST_ARGS)
	uv run coverage combine --quiet
	uv run coverage report

cov: test
	uv run coverage html
	@echo "Coverage report: htmlcov/index.html"

check: lint typecheck test
