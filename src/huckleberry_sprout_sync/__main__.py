from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from .config import ConfigError, load_config
from .sprout import SproutClient

if TYPE_CHECKING:
    from .sync import BackfillResult, SyncResult


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hb-st-sync",
        description="Sync Huckleberry baby tracking data into Sprout Track.",
    )
    parser.add_argument(
        "--date",
        type=lambda value: date.fromisoformat(value),
        default=None,
        help="Target date YYYY-MM-DD (default: previous day in the configured timezone)",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Print planned events without writing to Sprout Track",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Write to Sprout Track even if DRY_RUN is true in the environment",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    sync = subparsers.add_parser("sync", help="Sync one day of Huckleberry history into Sprout Track")
    sync.add_argument(
        "--date",
        type=lambda value: date.fromisoformat(value),
        default=None,
        help="Target date YYYY-MM-DD (default: previous day in the configured timezone)",
    )
    sync.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    sync.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    sync.add_argument(
        "--child",
        default=None,
        help="Huckleberry child to sync (name or nickname token). Default: first child.",
    )

    backfill = subparsers.add_parser(
        "backfill",
        help="One-time sync of all Huckleberry history from a start date through the end date (default: previous day)",
    )
    backfill.add_argument(
        "--start",
        type=lambda value: date.fromisoformat(value),
        default=None,
        help="First day to backfill YYYY-MM-DD (default: ~18 months ago)",
    )
    backfill.add_argument(
        "--end",
        type=lambda value: date.fromisoformat(value),
        default=None,
        help="Last day to backfill YYYY-MM-DD (default: previous day in the configured timezone)",
    )
    backfill.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    backfill.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    backfill.add_argument("--child", default=None)

    subparsers.add_parser(
        "sprout-info",
        help="Introspect the Sprout Track instance: list babies and valid reference values",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config(dry_run=args.dry_run)
    except ConfigError as exc:
        parser.error(str(exc))

    command = args.command or "sync"

    if command == "sprout-info":
        return asyncio.run(_show_sprout_info(cfg))

    target = args.date or cfg.local_today - timedelta(days=1)
    return asyncio.run(_run_sync_with_notify(cfg, args, command, target))


async def _show_sprout_info(cfg) -> int:
    sprout = SproutClient(cfg)
    try:
        babies = await sprout.list_babies()
        print("babies:")
        for baby in babies:
            print(f"  {baby.get('firstName') or '?'}  id={baby.get('id')}  age={baby.get('ageFormatted')}")
        if not babies:
            print("  (none)")
        baby_id = babies[0]["id"] if babies else None
        if baby_id:
            reference = await sprout.get_reference(baby_id)
            for key, value in sorted(reference.items()):
                print(f"{key}:")
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            print(f"  - {item.get('unitAbbr') or item.get('value') or item.get('name')}")
                        else:
                            print(f"  - {item}")
                elif isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        print(f"  {sub_key}: {sub_value}")
        return 0
    finally:
        await sprout.close()


_KIND_ORDER = {
    "feed": 0,
    "sleep": 1,
    "diaper": 2,
    "play": 3,
    "bath": 4,
    "measurement": 5,
    "pump": 6,
    "medicine": 7,
    "supplement": 8,
}


async def _run_sync_with_notify(cfg, args, command: str, target: date) -> int:
    from .notify import ERROR_TITLE, OK_TITLE, AppriseNotifier

    notifier = AppriseNotifier(cfg.apprise_url)
    try:
        if command == "backfill":
            from .sync import backfill

            start = args.start or cfg.local_today - timedelta(days=550)
            result = await backfill(cfg, start, args.end, dry_run=args.dry_run, child=args.child)
            title, body = OK_TITLE, _format_backfill(cfg, result)
            code = 1 if result.days_failed else 0
        else:
            from .sync import sync_day

            result = await sync_day(cfg, target, dry_run=args.dry_run, child=args.child)
            title, body = OK_TITLE, _format_sync(cfg, result)
            code = 0
        print(_result_json(command, result))
        if cfg.dry_run:
            return 0
        if not code:
            await notifier.send(title=title, body=body, message_type="success")
        return code
    except Exception as exc:
        if not cfg.dry_run:
            await notifier.send(title=ERROR_TITLE, body=f"{type(exc).__name__}: {exc}", message_type="failure")
        raise


def _format_sync(cfg, result: SyncResult) -> str:
    lines = [f"{result.day}  records={result.records}"]
    lines.append(_format_counts(result.by_kind))
    if not result.events:
        return "\n".join(lines)
    for event in sorted(result.events, key=lambda e: (e.time, _KIND_ORDER.get(e.sprout_type, 99))):
        when = event.time.astimezone(cfg.timezone).strftime("%H:%M")
        lines.append(f"- {when} {event.sprout_type} {event.summary}")
    if result.skipped_by_type:
        skipped = ", ".join(f"{kind}={count}" for kind, count in sorted(result.skipped_by_type.items()))
        lines.append(f"\nskipped (already in Sprout Track): {skipped}")
    lines.append(f"planned={result.planned} written={result.written}")
    return "\n".join(lines)


def _format_backfill(cfg, result: BackfillResult) -> str:
    lines = [
        f"range {result.start}..{result.end}",
        f"days={result.days} failed={result.days_failed} records={result.records} planned={result.planned}",
    ]
    if result.written_by_type:
        breakdown = ", ".join(f"{kind}={count}" for kind, count in sorted(result.written_by_type.items()))
        lines.append(f"written={result.written}  [{breakdown}]")
    else:
        lines.append(f"written={result.written}")
    return "\n".join(lines)


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "records=0"
    return " ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))


def _result_json(command: str, result) -> str:
    import json

    if command == "backfill":
        payload = {
            "complete": 1,
            "code": 1 if result.days_failed else 0,
            "command": command,
            "start": result.start.isoformat(),
            "end": result.end.isoformat(),
            "dryRun": result.dry_run,
            "days": result.days,
            "daysFailed": result.days_failed,
            "records": result.records,
            "planned": result.planned,
            "written": result.written,
            "writtenByType": dict(result.written_by_type),
        }
    else:
        payload = {
            "complete": 1,
            "code": 0,
            "command": command,
            "day": result.day.isoformat(),
            "dryRun": result.dry_run,
            "records": result.records,
            "byKind": dict(result.by_kind),
            "planned": result.planned,
            "written": result.written,
            "writtenByType": dict(result.written_by_type),
            "skippedByType": dict(result.skipped_by_type),
        }
    return json.dumps(payload)


if __name__ == "__main__":
    raise SystemExit(main())
