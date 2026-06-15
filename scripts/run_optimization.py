#!/usr/bin/env python3
"""Run Optuna parameter optimization."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console

from scalper.optimize import run_optimization

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Optimize scalper parameters with Optuna")
    parser.add_argument("--config", required=True, help="Path to YAML config")
    parser.add_argument("--data", required=True, help="Path to CSV bar data")
    parser.add_argument("--trials", type=int, default=25, help="Number of Optuna trials")
    parser.add_argument("--out", default="data/reports/optimization", help="Output directory")
    parser.add_argument(
        "--focus",
        choices=["all", "exit", "entry", "filters"],
        default="all",
        help="Parameter search scope: all, exit-only, entry-only, or entry+filter tuning",
    )
    parser.add_argument(
        "--scalping",
        action="store_true",
        help="Use wider stop/target ranges appropriate for sub-minute scalping (SL 8-40 ticks)",
    )
    args = parser.parse_args()

    config_path = ROOT / args.config if not Path(args.config).is_absolute() else Path(args.config)
    data_path = ROOT / args.data if not Path(args.data).is_absolute() else Path(args.data)
    out_dir = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = run_optimization(config_path, data_path, n_trials=args.trials, focus=args.focus, scalping=args.scalping)
    out_path = out_dir / "optimization_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    console.print(f"[green]Optimization complete ({args.trials} trials)[/green]")
    console.print(f"Best {result['metric']}: {result['best_value']:.4f}")
    console.print(f"Best params: {result['best_params']}")
    console.print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
