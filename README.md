# huckleberry-sprouttrack-sync

Syncs baby tracking data from [Huckleberry](https://huckleberry.com/) into a
self-hosted [Sprout Track](https://github.com/Oak-and-Sprout/sprout-track)
instance (e.g. `https://sprout-track.ngoclab.com`) through its webhook API.

Huckleberry has no official read API — history is read directly from its
Firebase Firestore database (the same transport the Huckleberry app uses, via
the reverse-engineered [huckleberry-api](https://github.com/Woyken/py-huckleberry-api)
library). Sprout Track is written to over its documented webhook API
(`/api/hooks/v1`) with an API key created in the app. Both halves are
unofficial integrations and can break if either service changes.

This is the mirror project of `codmon-huckleberry-sync` (Codmon → Huckleberry):
it makes Huckleberry the source of truth that also feeds Sprout Track.

## What gets synced

| Huckleberry record | Sprout Track activity |
| --- | --- |
| bottle feed | `feed` (`formula`/`breast milk`/`milk`/`other`, amount + unit) |
| solids feed | `feed` `SOLIDS` (foods joined into `food`, notes) |
| breast feed | `feed` `BREAST` with duration (minutes), starts a timed feed |
| sleep | `sleep` `log` (start time + duration; `NAP` or `NIGHT_SLEEP`) |
| diaper | `diaper` `WET`/`DIRTY`/`BOTH`/`DRY` (+ condition/color) |
| activities | `play` (`TUMMY_TIME`/`INDOOR_PLAY`/`OUTDOOR_PLAY`/`CUSTOM`) |
| bath activity | `bath` (`Full Bath`) |
| temperature | `measurement` `TEMPERATURE` |

Sleep durations are rounded to whole minutes (the webhook API only accepts a
`duration` in minutes and derives `endTime` from the `time` you send). Sleeps
that *start* at or after `NIGHT_START_HOUR` (default 20:00) are typed
`NIGHT_SLEEP`, earlier ones `NAP`.

## How it works

1. **Read** — log in to Huckleberry and stream the `feed`/`sleep`/`activities`/
   `diaper`/`health` interval subcollections for the target day(s) from
   Firestore, normalizing rows into records.
2. **Map** — records become Sprout Track webhook payloads (see table above).
   Bottle amounts are converted from Huckleberry's stored unit to the Sprout
   Track family's configured unit (`SPROUT_UNIT`, default `ML` when available):
   1 fl oz = 29.5735 ml.
3. **Dedupe** — before writing, the live Sprout Track activity history is
   polled (`GET /activities?type=…&since=…`) and an event is skipped when a
   same-type activity already exists within `DEDUP_WINDOW_MINUTES` (default
   15) of its planned time. This keeps syncs idempotent and makes `--force`
   replay safe.
4. **Write** — events are POSTed to `/api/hooks/v1/babies/:id/activities`,
   throttled to stay under the 30 writes/minute rate limit and retried with
   backoff on 429s.
5. **State** — `data/state.json` records which dates were already synced so
   the daily run skips cleanly.

## Local setup

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.14.

```bash
uv sync
cp .env.example .env
# fill in your credentials
```

### Get the Sprout Track API key and baby id (once)

Create the key in the Sprout Track app: **Settings → Admin → Integrations**
(admin role). Keys look like `st_live_…` and are shown **once** — copy it
immediately. Then discover the baby id:

```bash
uv run hb-st-sync sprout-info
```

Put the key in `SPROUT_API_KEY` (and optionally `SPROUT_BABY_ID`) in `.env`.

### Check what would be synced (dry run)

```bash
uv run hb-st-sync sync --date 2026-10-05                    # dry run by default (DRY_RUN=true)
uv run hb-st-sync sync --date 2026-10-05 --no-dry-run
uv run hb-st-sync sync --date 2026-10-05 --force --no-dry-run   # re-sync a day
```

Without `--date`, today is used (in `TIMEZONE`, default `Asia/Tokyo`).

### Backfill history

```bash
uv run hb-st-sync backfill --start 2025-04-01 --dry-run     # plan only
uv run hb-st-sync backfill --start 2025-04-01 --no-dry-run  # write
```

## Docker

Prefer not to manage a Python virtualenv? Run it as a container:

```bash
docker build -t huckleberry-sprouttrack-sync:latest .
```

The image runs the same `sync` command by default. Mount a directory for
`DATA_DIR` so `data/state.json` persists between runs:

```bash
docker run --rm \
  --env-file .env \
  -v hb-st-sync-data:/data \
  -e DATA_DIR=/data \
  huckleberry-sprouttrack-sync:latest sync
```

Set `DRY_RUN=false` (env or `-e`) once you want real writes.

## Environment variables

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `HUCKLEBERRY_EMAIL` | yes | — | Huckleberry login email (source) |
| `HUCKLEBERRY_PASSWORD` | yes | — | Huckleberry login password |
| `SPROUT_BASE_URL` | no | `https://sprout-track.ngoclab.com` | Sprout Track instance base URL |
| `SPROUT_API_KEY` | yes | — | Sprout Track webhook key (`st_live_…`, Settings → Admin → Integrations) |
| `SPROUT_BABY_ID` | no | first baby | Baby to write; auto-resolved from `GET /babies` when unset |
| `TIMEZONE` | no | `Asia/Tokyo` | IANA timezone used for event timestamps |
| `DRY_RUN` | no | `true` | Plan only; do not write to Sprout Track |
| `CHILD` | no | first child | Huckleberry child name/nickname token to read |
| `SYNC_FEED` | no | `true` | Sync bottles/solids/breast feeds |
| `SYNC_SLEEP` | no | `true` | Sync sleeps |
| `SYNC_DIAPER` | no | `true` | Sync diapers |
| `SYNC_ACTIVITY` | no | `true` | Sync play + bath activities |
| `SYNC_TEMPERATURE` | no | `true` | Sync temperatures as measurements |
| `SPROUT_UNIT` | no | `ML` | Volume unit to send; must be configured in the family (`ML`/`OZ`) |
| `NIGHT_START_HOUR` | no | `20` | Sleeps starting at/after this hour are `NIGHT_SLEEP` |
| `DEDUP_WINDOW_MINUTES` | no | `15` | Skip if matching Sprout Track activity exists within this window |
| `DEDUP_SINCE_DAYS` | no | `7` | How far back to poll Sprout Track for dedup, relative to the earliest day |
| `WRITE_DELAY_SECONDS` | no | `2.2` | Minimum pause between POSTs (30/min write rate limit) |
| `APPRISE_URL` | no | — | Apprise webhook; posts a success/failure notification after each real sync |
| `DATA_DIR` | no | `data` | State directory |

## Disclaimer / risk

- Uses your Huckleberry credentials to read Firestore and a Sprout Track API key
  to write; both are reverse-engineered / non-official integrations. Use at
  your own risk.
- Sprout Track writes create permanent entries. Start with dry-run mode and
  verify planned events before enabling real sync.
- Credentials are read from the environment; keep `.env` out of version control.