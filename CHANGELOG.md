# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial release: sync Huckleberry baby-tracking history into a self-hosted
  [Sprout Track](https://github.com/Oak-and-Sprout/sprout-track) instance over
  its webhook API.
- `hb-st-sync sync` (previous day by default, or `--date`), `hb-st-sync backfill
  --start`, `hb-st-sync sprout-info`, `hb-st-sync compare --db`, and
  `hb-st-sync reconcile --db` subcommands.
- Record mappings for bottle, solids, and breast feeds; sleep; diaper;
  play/bath activities; temperature; pump; growth (weight/height/head); and
  medications matched against Sprout Track's configured medicines.
- Amount unit conversion between Huckleberry's stored unit and the Sprout Track
  family unit (`SPROUT_UNIT`, `ML`/`OZ`).
- Idempotent writes: existing-activity dedup within a configurable window, plus
  `--force` to re-sync a day.
- Rate-limit-aware writes throttled to 30/minute per API key, round-robined
  across multiple `SPROUT_API_KEYS`, with backoff on HTTP 429.
- `hb-st-sync reconcile`: compare-driven gap fill that POSTs only truly-missing
  events against a Sprout DB snapshot (dry-run by default, re-run safe), with
  opt-in `--update-drift` / `--update-moved` PUTs and an idle-instance warm-up
  before writes.
- Dry-run by default (`DRY_RUN=true`); result JSON emitted on stdout and a
  non-zero exit when a backfill has failed days.
- Optional Apprise webhook notifications after a real sync.
- Container image (`Dockerfile`).

[Unreleased]: https://github.com/ngoc-minh-do/huckleberry-sprouttrack-sync/commits/main
