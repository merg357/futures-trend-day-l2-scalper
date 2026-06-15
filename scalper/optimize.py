"""Optuna hyperparameter optimization."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import optuna

from scalper.backtest import run_backtest
from scalper.config import ScalperConfig, load_config


def _objective_value(metrics: Any, metric_name: str) -> float:
    if metric_name == "profit_factor":
        return float(metrics.profit_factor)
    if metric_name == "net_pnl":
        return float(metrics.net_pnl)
    if metric_name == "win_rate":
        return float(metrics.win_rate)
    if metric_name == "sharpe_ratio":
        return float(metrics.sharpe_ratio)
    return float(metrics.profit_factor)


def suggest_params(
    trial: optuna.Trial,
    base: ScalperConfig,
    focus: str = "all",
    *,
    scalping: bool = False,
    stop_loss_min: int | None = None,
    stop_loss_max: int | None = None,
) -> ScalperConfig:
    cfg = deepcopy(base)
    if focus in ("all", "entry", "filters"):
        if focus == "filters":
            cfg.trend.min_trend_score = trial.suggest_int("min_trend_score", 60, 90)
            cfg.l2.min_l2_score = trial.suggest_int("min_l2_score", 55, 85)
            cfg.trend.adx_trend_min = trial.suggest_int("adx_trend_min", 15, 35)
            cfg.trend.atr_expansion_mult = trial.suggest_float("atr_expansion_mult", 0.9, 1.5)
            cfg.l2.imbalance_threshold = trial.suggest_float("imbalance_threshold", 0.45, 0.75)
            cfg.l2.min_book_depth = trial.suggest_int("min_book_depth", 20, 150)
            cfg.entry.pullback_to_ema_ticks = trial.suggest_int("pullback_to_ema_ticks", 1, 6)
            cfg.entry.max_spread_ticks = trial.suggest_int("max_spread_ticks", 1, 6)
        elif scalping and focus == "all":
            cfg.trend.min_trend_score = trial.suggest_int("min_trend_score", 50, 80)
            cfg.l2.min_l2_score = trial.suggest_int("min_l2_score", 45, 75)
            cfg.trend.adx_trend_min = trial.suggest_int("adx_trend_min", 18, 32)
            cfg.trend.atr_expansion_mult = trial.suggest_float("atr_expansion_mult", 0.95, 1.4)
            cfg.l2.imbalance_threshold = trial.suggest_float("imbalance_threshold", 0.48, 0.72)
            cfg.l2.min_book_depth = trial.suggest_int("min_book_depth", 25, 120)
            cfg.entry.pullback_to_ema_ticks = trial.suggest_int("pullback_to_ema_ticks", 1, 5)
            cfg.entry.max_spread_ticks = trial.suggest_int("max_spread_ticks", 1, 4)
        else:
            cfg.trend.min_trend_score = trial.suggest_int("min_trend_score", 45, 75)
            cfg.l2.min_l2_score = trial.suggest_int("min_l2_score", 40, 70)
            cfg.entry.pullback_to_ema_ticks = trial.suggest_int("pullback_to_ema_ticks", 1, 5)
    if focus in ("all", "exit"):
        sl_min = stop_loss_min if stop_loss_min is not None else (8 if scalping else 6)
        sl_max = stop_loss_max if stop_loss_max is not None else 40
        if scalping:
            cfg.exit.stop_loss_ticks = trial.suggest_int("stop_loss_ticks", sl_min, sl_max)
            cfg.exit.take_profit_ticks = trial.suggest_int("take_profit_ticks", 12, 50)
            cfg.exit.breakeven_trigger_ticks = trial.suggest_int("breakeven_trigger_ticks", 6, 20)
            cfg.exit.trailing_trigger_ticks = trial.suggest_int("trailing_trigger_ticks", 10, 30)
            cfg.exit.trailing_offset_ticks = trial.suggest_int("trailing_offset_ticks", 4, 15)
            cfg.exit.max_hold_bars = trial.suggest_int("max_hold_bars", 30, 180)
        else:
            cfg.exit.stop_loss_ticks = trial.suggest_int("stop_loss_ticks", sl_min, min(sl_max, 16) if stop_loss_min is None else sl_max)
            cfg.exit.take_profit_ticks = trial.suggest_int("take_profit_ticks", 10, 30)
            cfg.exit.breakeven_trigger_ticks = trial.suggest_int("breakeven_trigger_ticks", 4, 12)
            cfg.exit.trailing_trigger_ticks = trial.suggest_int("trailing_trigger_ticks", 8, 18)
            cfg.exit.trailing_offset_ticks = trial.suggest_int("trailing_offset_ticks", 4, 10)
            cfg.exit.max_hold_bars = trial.suggest_int("max_hold_bars", 20, 60)
    return cfg


def run_optimization(
    config_path: str | Path,
    data_path: str | Path,
    n_trials: int = 25,
    storage: str | None = None,
    focus: str = "all",
    *,
    scalping: bool = False,
    stop_loss_min: int | None = None,
    stop_loss_max: int | None = None,
    backtest_fn: Any | None = None,
) -> dict[str, Any]:
    base = load_config(config_path)
    metric_name = base.optimize.metric
    min_trades = base.optimize.min_trades
    run_bt = backtest_fn or run_backtest

    def objective(trial: optuna.Trial) -> float:
        cfg = suggest_params(
            trial,
            base,
            focus=focus,
            scalping=scalping,
            stop_loss_min=stop_loss_min,
            stop_loss_max=stop_loss_max,
        )
        result = run_bt(cfg, data_path, config_path=str(config_path))
        if result.metrics.total_trades < min_trades:
            return -1.0
        return _objective_value(result.metrics, metric_name)

    study = optuna.create_study(direction="maximize", storage=storage, load_if_exists=bool(storage))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_trial
    best_cfg = deepcopy(base)
    param_map = {
        "min_trend_score": ("trend", "min_trend_score"),
        "min_l2_score": ("l2", "min_l2_score"),
        "adx_trend_min": ("trend", "adx_trend_min"),
        "atr_expansion_mult": ("trend", "atr_expansion_mult"),
        "imbalance_threshold": ("l2", "imbalance_threshold"),
        "min_book_depth": ("l2", "min_book_depth"),
        "max_spread_ticks": ("entry", "max_spread_ticks"),
        "stop_loss_ticks": ("exit", "stop_loss_ticks"),
        "take_profit_ticks": ("exit", "take_profit_ticks"),
        "pullback_to_ema_ticks": ("entry", "pullback_to_ema_ticks"),
        "breakeven_trigger_ticks": ("exit", "breakeven_trigger_ticks"),
        "trailing_trigger_ticks": ("exit", "trailing_trigger_ticks"),
        "trailing_offset_ticks": ("exit", "trailing_offset_ticks"),
        "max_hold_bars": ("exit", "max_hold_bars"),
    }
    for k, v in best.params.items():
        if k in param_map:
            section, attr = param_map[k]
            setattr(getattr(best_cfg, section), attr, v)

    final = run_bt(best_cfg, data_path, config_path=str(config_path))

    return {
        "best_params": best.params,
        "best_value": best.value,
        "metric": metric_name,
        "focus": focus,
        "scalping": scalping,
        "stop_loss_min": stop_loss_min,
        "stop_loss_max": stop_loss_max,
        "n_trials": n_trials,
        "final_metrics": final.metrics.model_dump(),
        "final_trades": final.metrics.total_trades,
    }
