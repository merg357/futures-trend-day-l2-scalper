#!/usr/bin/env python3
"""Regenerate HTML/PNG reports from an existing report.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console

from scalper.models import BacktestResult
from scalper.reports import generate_report

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild reports from report.json")
    parser.add_argument("--json", required=True, help="Path to report.json")
    parser.add_argument("--out", help="Output directory (defaults to json parent)")
    args = parser.parse_args()

    json_path = ROOT / args.json if not Path(args.json).is_absolute() else Path(args.json)
    out_dir = Path(args.out) if args.out else json_path.parent
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    with json_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    result = BacktestResult.model_validate(data)
    paths = generate_report(result, out_dir)
    console.print(f"[green]Reports regenerated in {out_dir}[/green]")
    for k, p in paths.items():
        if k != "charts":
            console.print(f"  {k}: {p}")


if __name__ == "__main__":
    main()
