#!/usr/bin/env python3
"""Convert Rithmic L2 archives to scalper CSV bars (1m or sub-minute)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console
from rich.table import Table

from scalper.l2_etl import INTERVAL_SECONDS, convert_archive_to_bars, default_output_dir, write_manifest


def merge_manifest_file(manifest_path: Path, new_results: list) -> None:
    existing: list[dict] = []
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", [])
    by_key = {(f["instrument"], f["date"], f.get("interval", "1m")): f for f in existing}
    for r in new_results:
        by_key[(r.instrument, r.date, r.interval)] = r.to_dict()
    merged = sorted(by_key.values(), key=lambda x: (x["instrument"], x["date"], x.get("interval", "1m")))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"files": merged}, indent=2), encoding="utf-8")

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert L2 tar.gz archives to scalper CSV bars")
    parser.add_argument("--archive", help="Path to a single .tar.gz archive")
    parser.add_argument("--instrument", help="Instrument symbol, e.g. MNQ or MES")
    parser.add_argument("--date", help="Archive date YYYYMMDD")
    parser.add_argument("--out", help="Output directory (default: data/raw or data/raw/submin)")
    parser.add_argument(
        "--interval",
        default="1m",
        choices=sorted(INTERVAL_SECONDS),
        help="Bar interval: 10s, 15s, 30s, or 1m",
    )
    parser.add_argument("--include-depth", action="store_true", help="Also scan depth_updates parquets (slower)")
    parser.add_argument("--manifest", default="data/raw/etl_manifest.json", help="ETL manifest path")
    args = parser.parse_args()

    interval = args.interval.lower()
    default_out = default_output_dir(interval, ROOT / "data" / "raw")
    out_dir = Path(args.out) if args.out else default_out
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    manifest_path = ROOT / args.manifest if not Path(args.manifest).is_absolute() else Path(args.manifest)

    if args.archive:
        archive = Path(args.archive)
        if not archive.is_absolute():
            archive = ROOT / archive
        stem = archive.name.removesuffix(".tar.gz").removesuffix(".tar")
        instrument = (args.instrument or archive.parent.name).upper()
        date = args.date or stem
        jobs = [(instrument, date, archive)]
    elif args.instrument and args.date:
        jobs = [(args.instrument.upper(), args.date, None)]
    else:
        parser.error("Provide --archive or both --instrument and --date")

    results = []
    table = Table(title=f"L2 to {interval} CSV conversion")
    table.add_column("File")
    table.add_column("Rows")
    table.add_column("L2 real")
    table.add_column("Depth L1-L5")

    for instrument, date, archive in jobs:
        console.print(f"[cyan]Converting {instrument} {date} @ {interval}...[/cyan]")
        result = convert_archive_to_bars(
            instrument,
            date,
            archive=archive,
            output_dir=out_dir,
            include_depth=args.include_depth,
            interval=interval,
        )
        results.append(result)
        depth_note = f"L{result.depth_levels_available} real, L{result.depth_levels_approximated} approx"
        table.add_row(Path(result.output_path).name, str(result.row_count), str(result.l2_real), depth_note)
        if result.warnings:
            console.print(f"[yellow]{len(result.warnings)} warnings (showing first)[/yellow]")
            console.print(result.warnings[0])

    merge_manifest_file(manifest_path, results)
    console.print(table)
    console.print(f"[green]Manifest written to {manifest_path}[/green]")


if __name__ == "__main__":
    main()
