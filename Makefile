# triage-verse developer tasks. Mirrors the commands run in
# .github/workflows/ci.yml so the full check suite is runnable locally.
# Run `make` (or `make help`) to list targets.

PYTHON_SRC := src tests
TRIAGE_SCRIPTS := \
	.github/triage/scripts/resolve-repositories.py \
	.github/triage/scripts/resolve-label-specs.py \
	.github/triage/scripts/check-engine-guardrails.py

.PHONY: py-setup
py-setup:  ## [py] Install the project and dev tools
	@echo "📦 Installing project"
	uv sync

.PHONY: py-check
py-check: py-check-format py-check-types py-check-tests  ## [py] Run all Python checks

.PHONY: py-check-format
py-check-format:  ## [py] Check formatting and lint (ruff)
	@echo "📐 Checking format and lint with ruff"
	uv run ruff format --check $(PYTHON_SRC)
	uv run ruff check $(PYTHON_SRC)

.PHONY: py-check-types
py-check-types:  ## [py] Check types (pyright)
	@echo "📝 Checking types with pyright"
	uv run pyright

.PHONY: py-check-tests
py-check-tests:  ## [py] Run tests (pytest)
	@echo "🧪 Running tests with pytest"
	uv run pytest

.PHONY: py-format
py-format:  ## [py] Auto-fix lint and format Python code
	uv run ruff check --fix $(PYTHON_SRC)
	uv run ruff format $(PYTHON_SRC)

.PHONY: validate-yaml
validate-yaml:  ## Validate triage config YAML
	@echo "🧾 Validating triage YAML"
	uv run python .github/triage/scripts/validate-yaml.py

.PHONY: compile-scripts
compile-scripts:  ## Byte-compile the triage helper scripts
	@echo "🐍 Compiling triage scripts"
	uv run python -m py_compile $(TRIAGE_SCRIPTS)

.PHONY: js-check
js-check:  ## Syntax-check and test the Node triage tooling
	@echo "🟨 Checking Node triage tooling"
	node --check .github/triage/scripts/create-github-app-token-map.mjs
	node --check .github/triage/scripts/gh-token-router.mjs
	node --check .github/triage/scripts/gh-aw-output-adapter.mjs
	node --check .github/triage/scripts/dry-run-triage-actions.mjs
	node --check .github/triage/scripts/process-triage-actions.mjs
	node --test \
		tests/test_process_triage_actions.mjs \
		tests/test_gh_token_router.mjs \
		tests/test_gh_aw_output_adapter.mjs \
		tests/test_dry_run_triage_actions.mjs

.PHONY: check
check: validate-yaml compile-scripts py-check js-check  ## Run everything CI runs

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; { \
		printf "\033[32m%-18s\033[0m", $$1; \
		if ($$2 ~ /^\[py\]/) { \
			printf "  \033[31m[py]\033[0m%s\n", substr($$2, 5); \
		} else { \
			printf "       %s\n", $$2; \
		} \
	}'

.DEFAULT_GOAL := help
