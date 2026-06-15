#!/usr/bin/env python3
"""Generate synthetic sample datasets for backtesting."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console
from scalper.sample_data import generate_all_samples

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate sample futures bar data")
    parser.add_argument("--out", default="data/sample", help="Output directory")
    parser.add_argument("--bars", type=int, default=120, help="Bars per dataset")
    args = parser.parse_args()

    out_dir = ROOT / args.out
    paths = generate_all_samples(out_dir, n_bars=args.bars)
    console.print(f"[green]Generated {len(paths)} datasets in {out_dir}[/green]")
    for name, p in paths.items():
        console.print(f"  • {name}")


if __name__ == "__main__":
    main()
