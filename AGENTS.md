# AGENTS.md

Instructions for AI coding agents working in this repository.

## Commands

- Sync/install dependencies: `uv sync`
- Full gate — run before finishing any change: `make check`
  - `make lint` / `make format-check` (ruff), `make typecheck` (ty), `make test` (pytest)
- Auto-fix lint and formatting: `make fix`
- Install git hooks: `make install`
- Audit locked dependencies: `make audit`

Use `uv` for all Python and package operations (never bare `pip`/`venv`).

## Conventions

- Commit messages follow Conventional Commits (`feat:`, `fix:`, ...); the
  `commit-msg` hook validates them with commitizen.
- Match the existing style: ruff line length 120, `from __future__ import
  annotations`, modern `X | None` syntax, module-level
  `logging.getLogger(__name__)`.
- Configuration comes from environment variables (see
  `src/huckleberry_sprout_sync/config.py`); never commit secrets or a real
  `.env`.
- Keep `README.md` and `CHANGELOG.md` in sync with behavior changes; add tests
  under `tests/` mirroring the package layout.
