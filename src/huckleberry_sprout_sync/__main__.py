from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import defaultdict
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

    compare = subparsers.add_parser(
        "compare",
        help="Read-only diff of Huckleberry history against a Sprout Track DB snapshot",
    )
    compare.add_argument("--db", required=True, help="Path to a Sprout Track baby-tracker.db snapshot")
    compare.add_argument("--start", type=lambda value: date.fromisoformat(value), default=None)
    compare.add_argument("--end", type=lambda value: date.fromisoformat(value), default=None)
    compare.add_argument("--child", default=None)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="POST only the events truly missing from a Sprout Track DB snapshot (dry run by default)",
    )
    reconcile.add_argument("--db", required=True, help="Path to a fresh Sprout Track baby-tracker.db snapshot")
    reconcile.add_argument("--start", type=lambda value: date.fromisoformat(value), default=None)
    reconcile.add_argument("--end", type=lambda value: date.fromisoformat(value), default=None)
    reconcile.add_argument(
        "--update-drift",
        action="store_true",
        help="With --no-dry-run, PUT rows whose fields differ from Huckleberry",
    )
    reconcile.add_argument(
        "--update-moved",
        action="store_true",
        help="With --no-dry-run, PUT rows whose start time shifted",
    )
    reconcile.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    reconcile.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    reconcile.add_argument("--child", default=None)
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

    if command == "compare":
        return asyncio.run(_run_compare(cfg, args))

    if command == "reconcile":
        return asyncio.run(_run_reconcile(cfg, args))

    target = args.date or cfg.local_today - timedelta(days=1)
    return asyncio.run(_run_sync_with_notify(cfg, args, command, target))


async def _run_compare(cfg, args) -> int:
    from .compare import collect, format_report
    from .hb_reader import HuckleberryReader

    end = args.end or cfg.local_today
    start = args.start or end - timedelta(days=550)
    reader = HuckleberryReader(cfg)
    await reader.connect(child=args.child or cfg.child)
    try:
        result = await collect(reader, cfg, args.db, start, end)
    finally:
        await reader.close()
    print(format_report(result, cfg.timezone, start, end))
    return 0


async def _run_reconcile(cfg, args) -> int:
    from .notify import ERROR_TITLE, OK_TITLE, AppriseNotifier
    from .reconcile import reconcile

    notifier = AppriseNotifier(cfg.apprise_url)
    end = args.end or cfg.local_today - timedelta(days=1)
    start = args.start or end - timedelta(days=550)
    try:
        result = await reconcile(
            cfg,
            args.db,
            start,
            end,
            dry_run=args.dry_run,
            update_drift=args.update_drift,
            update_moved=args.update_moved,
            child=args.child,
        )
        print(_format_reconcile(cfg, result))
        print(_reconcile_json(result))
        if not cfg.dry_run:
            await notifier.send(title=OK_TITLE, body=_format_reconcile(cfg, result), message_type="success")
        return 0
    except Exception as exc:
        # Under Dagu the DAG-level handler owns failure reporting (it sends the
        # rich step+log message), so skip our own to avoid duplicate alerts.
        if not cfg.dry_run and not os.environ.get("DAG_NAME"):
            await notifier.send(title=ERROR_TITLE, body=f"{type(exc).__name__}: {exc}", message_type="failure")
        raise


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
    "food": 1,
    "sleep": 2,
    "diaper": 3,
    "play": 4,
    "bath": 5,
    "measurement": 6,
    "pump": 7,
    "medicine": 8,
    "supplement": 9,
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
        # Under Dagu the DAG-level handler owns failure reporting (it sends the
        # rich step+log message), so skip our own to avoid duplicate alerts.
        if not cfg.dry_run and not os.environ.get("DAG_NAME"):
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


def _format_reconcile(cfg, result) -> str:
    mode = "DRY RUN" if result.dry_run else "WRITE"
    lines = [
        f"=== reconcile {result.start}..{result.end} ({cfg.timezone.key}) — {mode} ===",
        (
            f"HB planned={result.hb_total} sprout={result.sprout_total} matched={result.matched} "
            f"drift={result.drift} moved={result.moved} near_dup={result.near_dup} "
            f"truly_missing={result.truly_missing} sprout_only={result.sprout_only}"
        ),
    ]
    if result.to_post:
        by_day: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        for event in result.to_post:
            by_day[f"{event.time.astimezone(cfg.timezone):%Y-%m-%d}"] += 1
            by_type[event.sprout_type] += 1
        lines.append("missing by day: " + ", ".join(f"{day}={count}" for day, count in sorted(by_day.items())))
        lines.append("missing by type: " + ", ".join(f"{kind}={count}" for kind, count in sorted(by_type.items())))
    put_lines: list[str] = []
    if result.update_drift:
        put_lines.append(f"drift={result.drift}")
    if result.update_moved:
        put_lines.append(f"moved={result.moved}")
    lines.append("to PUT: " + (", ".join(put_lines) if put_lines else "0"))
    if result.dry_run:
        lines.append("(dry run — nothing written; use --no-dry-run to apply)")
    else:
        breakdown = ", ".join(f"{kind}={count}" for kind, count in sorted(result.written_by_type.items()))
        updated = ", ".join(f"{kind}={count}" for kind, count in sorted(result.updated_by_type.items()))
        lines.append(f"written={result.written} [{breakdown}]  updated={result.updated} [{updated}]")
    return "\n".join(lines)


def _reconcile_json(result) -> str:
    import json

    payload = {
        "complete": 1,
        "code": 0,
        "command": "reconcile",
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "dryRun": result.dry_run,
        "snapshotDb": result.snapshot_db,
        "hbTotal": result.hb_total,
        "sproutTotal": result.sprout_total,
        "matched": result.matched,
        "drift": result.drift,
        "moved": result.moved,
        "nearDup": result.near_dup,
        "trulyMissing": result.truly_missing,
        "sproutOnly": result.sprout_only,
        "updateDrift": result.update_drift,
        "updateMoved": result.update_moved,
        "written": result.written,
        "updated": result.updated,
        "writtenByType": dict(result.written_by_type),
        "updatedByType": dict(result.updated_by_type),
    }
    return json.dumps(payload)


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
