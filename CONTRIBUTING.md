# Contributing

Thanks for your interest in improving `huckleberry-sprouttrack-sync`!

## Development setup

Requires [uv](https://docs.astral.sh/uv/). The Python version is pinned in
`.python-version` (3.14).

```bash
git clone https://github.com/ngoc-minh-do/huckleberry-sprouttrack-sync.git
cd huckleberry-sprouttrack-sync
uv sync
```

## Running the checks

Everything CI runs is available as a single command:

```bash
make check      # ruff lint + format check + ty typecheck + pytest
```

Individual targets: `make lint`, `make format`, `make format-check`,
`make typecheck`, `make test`, `make audit`. Auto-fix lint/format with
`make fix`.

## Commit messages

This project follows [Conventional Commits](https://www.conventionalcommits.org/).
Install the git hooks once so messages are validated before they are created:

```bash
make install    # uv sync + prek install (pre-commit + commit-msg hooks)
```

Format: `<type>(<scope>): <description>`, e.g.
`fix(sync): handle an empty bottle amount`. Common types: `feat`, `fix`,
`docs`, `refactor`, `test`, `chore`, `ci`.

## Pull requests

1. Branch off `main` (e.g. `git switch -c feat/thing`).
2. Make your change, add or update tests, and run `make check`.
3. Update `CHANGELOG.md` under `## [Unreleased]` when behavior changes.
4. Open a PR — CI runs the same checks and lints the commit messages.
5. PRs are **squash-merged** to keep `main` history linear.

## Code style

- Formatting and linting: [ruff](https://docs.astral.sh/ruff/) (line length 120).
- Type checking: [ty](https://docs.astral.sh/ty/).
- Tests: [pytest](https://docs.pytest.org/).

## Reporting issues

- Bugs and feature requests: open an issue using the provided templates.
- Security problems: report privately per [SECURITY.md](SECURITY.md) — never in
  a public issue.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
