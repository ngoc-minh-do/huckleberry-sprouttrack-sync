.DEFAULT_GOAL := help
SHELL := /bin/sh

.PHONY: help install lint format format-check typecheck test audit check fix precommit hooks-update build clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Sync dependencies and install git hooks
	uv sync
	uv run prek install --hook-type pre-commit --hook-type commit-msg

lint: ## Lint with ruff
	uv run ruff check .

format: ## Format with ruff
	uv run ruff format .

format-check: ## Check formatting with ruff
	uv run ruff format --check .

typecheck: ## Type-check with ty
	uv run ty check

test: ## Run the test suite
	@uv run pytest; status=$$?; if [ $$status -eq 5 ]; then exit 0; else exit $$status; fi

audit: ## Audit locked dependencies for known vulnerabilities
	uv audit

check: lint format-check typecheck test ## Run the full CI gate locally

fix: ## Auto-fix lint and formatting
	uv run ruff check --fix .
	uv run ruff format .

precommit: ## Run all pre-commit hooks against the whole tree
	uv run prek run --all-files

hooks-update: ## Update pinned pre-commit hook revisions
	uv run prek auto-update

build: ## Build the sdist and wheel
	uv build

clean: ## Remove build and cache artifacts
	rm -rf dist build .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
