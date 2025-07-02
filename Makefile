.PHONY: all
all: lint test

.PHONY: test
test:
	uv run pytest

.PHONY: lint
lint:
	uv run pre-commit install
	uv run pre-commit run --all-files
	uv run mypy

.PHONY: clean
	git clean -f
