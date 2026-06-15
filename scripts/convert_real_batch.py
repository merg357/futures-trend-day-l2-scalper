#!/usr/bin/env python3
"""Batch convert L2 archive days for real-data backtests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scalper.l2_etl import INTERVAL_SECONDS, convert_archive_to_bars, default_output_dir

INVENTORY_PATH = Path(r"D:\AI_Vault\futures-options-backtest-lab\results\tradedata_inventory.json")
INSTRUMENTS = ("MNQ", "MES")
SUBMIN_INTERVALS = ("10s", "15s", "30s")


def load_inventory_dates() -> dict[str, list[str]]:
    if not INVENTORY_PATH.is_file():
        raise FileNotFoundError(f"Inventory not found: {INVENTORY_PATH}")
    payload = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    merged = payload.get("merged", {})
    out: dict[str, list[str]] = {}
    for inst in INSTRUMENTS:
        key = f"l2/{inst}"
        dates = merged.get(key, {}).get("dates", [])
        out[inst] = sorted(dates)
    return out


def load_manifest_dates(manifest_path: Path, instrument: str) -> list[str]:
    if not manifest_path.is_file():
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    dates = sorted(
        {
            f["date"]
            for f in payload.get("files", [])
            if f.get("instrument") == instrument and f.get("interval", "1m") == "1m"
        }
    )
    return dates


def load_converted_keys(manifest_path: Path, interval: str) -> set[tuple[str, str, str]]:
    if not manifest_path.is_file():
        return set()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        (f["instrument"], f["date"], f.get("interval", "1m"))
        for f in payload.get("files", [])
        if f.get("interval", "1m") == interval
    }


def merge_manifest(existing: list[dict], new_results: list) -> list[dict]:
    by_key = {(f["instrument"], f["date"], f.get("interval", "1m")): f for f in existing}
    for r in new_results:
        by_key[(r.instrument, r.date, r.interval)] = r.to_dict()
    return sorted(by_key.values(), key=lambda x: (x["instrument"], x["date"], x.get("interval", "1m")))


def convert_instrument(
    instrument: str,
    dates: list[str],
    out_dir: Path,
    skip_existing: bool,
    converted: set[tuple[str, str, str]],
    interval: str,
) -> tuple[list, list[str]]:
    results = []
    errors: list[str] = []
    for date in dates:
        key = (instrument, date, interval)
        csv_path = out_dir / f"{instrument}_{date}_{interval}.csv"
        if skip_existing and (key in converted or csv_path.is_file()):
            print(f"SKIP {instrument} {date} {interval}: already converted")
            continue
        try:
            result = convert_archive_to_bars(instrument, date, output_dir=out_dir, interval=interval)
            results.append(result)
            print(f"OK {instrument} {date} {interval} -> {result.row_count} bars")
        except Exception as exc:  # noqa: BLE001
            msg = f"{instrument} {date} {interval}: {exc}"
            errors.append(msg)
            print(f"ERR {msg}")
    return results, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch convert L2 archives to CSV bars")
    parser.add_argument("--all", action="store_true", help="Convert all inventory dates for MNQ and MES")
    parser.add_argument("--from-manifest", action="store_true", help="Use dates from etl_manifest 1m entries")
    parser.add_argument("--instrument", choices=INSTRUMENTS, action="append", help="Limit to instrument(s)")
    parser.add_argument("--dates", nargs="+", help="Specific YYYYMMDD dates")
    parser.add_argument(
        "--interval",
        default="1m",
        choices=sorted(INTERVAL_SECONDS),
        help="Bar interval (default 1m)",
    )
    parser.add_argument("--skip-existing", action="store_true", default=True)
    parser.add_argument("--force", action="store_true", help="Re-convert even if output exists")
    args = parser.parse_args()

    interval = args.interval.lower()
    raw_root = ROOT / "data" / "raw"
    out_dir = default_output_dir(interval, raw_root)
    manifest_path = raw_root / "etl_manifest.json"
    converted = load_converted_keys(manifest_path, interval)
    skip_existing = args.skip_existing and not args.force

    instruments = args.instrument or list(INSTRUMENTS)
    inventory = load_inventory_dates() if args.all else {}

    all_results: list = []
    all_errors: list[str] = []

    for inst in instruments:
        if args.dates:
            dates = sorted(args.dates)
        elif args.from_manifest:
            dates = load_manifest_dates(manifest_path, inst)
            print(f"{inst}: {len(dates)} dates from etl_manifest (1m)")
        elif args.all:
            dates = inventory.get(inst, [])
            print(f"{inst}: {len(dates)} inventory dates")
        else:
            dates = inventory.get(inst, [])[:7]
        results, errors = convert_instrument(inst, dates, out_dir, skip_existing, converted, interval)
        all_results.extend(results)
        all_errors.extend(errors)

    existing_files: list[dict] = []
    if manifest_path.is_file():
        existing_files = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", [])
    merged = merge_manifest(existing_files, all_results)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"files": merged}, indent=2), encoding="utf-8")

    error_log = raw_root / "etl_errors.log"
    if all_errors:
        error_log.write_text("\n".join(all_errors) + "\n", encoding="utf-8")
        print(f"Errors logged -> {error_log} ({len(all_errors)} failures)")

    print(f"Converted {len(all_results)} new {interval} files; manifest has {len(merged)} total entries")


if __name__ == "__main__":
    main()
