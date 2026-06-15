"""Entry rules: chop filter, long/short pullback entries."""

from __future__ import annotations

import pandas as pd

from scalper.config import EntryConfig, ScalperConfig
from scalper.l2_score import compute_l2_score
from scalper.models import Bias, EntrySignal, Side
from scalper.trend_score import compute_trend_score


def is_chop(row: pd.Series, cfg: EntryConfig) -> bool:
    if not cfg.chop_filter_enabled:
        return False
    adx_val = row.get("adx", 0.0)
    if pd.notna(adx_val) and adx_val <= cfg.chop_adx_max:
        return True
    atr_val = row.get("atr", 1.0)
    bar_range = row.get("bar_range", row["high"] - row["low"])
    if pd.notna(atr_val) and atr_val > 0 and bar_range < atr_val * cfg.chop_range_atr_mult:
        return True
    return False


def _pullback_to_ema(row: pd.Series, bias: Bias, tick_size: float, tolerance_ticks: int) -> bool:
    tol = tolerance_ticks * tick_size
    if bias == Bias.LONG:
        return row["low"] <= row["ema_fast"] + tol and row["close"] > row["ema_fast"]
    if bias == Bias.SHORT:
        return row["high"] >= row["ema_fast"] - tol and row["close"] < row["ema_fast"]
    return False


def evaluate_entry(
    row: pd.Series,
    prev_atr: float,
    bar_index: int,
    config: ScalperConfig,
    cooldown_remaining: int,
    session_bar_index: int,
) -> EntrySignal | None:
    if cooldown_remaining > 0:
        return None
    if session_bar_index < config.entry.min_bars_after_open:
        return None
    if is_chop(row, config.entry):
        return None

    bid = row.get("bid")
    ask = row.get("ask")
    if pd.notna(bid) and pd.notna(ask) and config.entry.max_spread_ticks > 0:
        spread_ticks = (float(ask) - float(bid)) / config.tick_size
        if spread_ticks > config.entry.max_spread_ticks:
            return None

    trend = compute_trend_score(row, prev_atr, config.trend)
    if trend.bias == Bias.NONE or trend.score < config.trend.min_trend_score:
        return None
    if not _pullback_to_ema(row, trend.bias, config.tick_size, config.entry.pullback_to_ema_ticks):
        return None

    side = Side.LONG if trend.bias == Bias.LONG else Side.SHORT
    l2 = compute_l2_score(row, side, config.l2)
    if config.entry.require_l2_confirmation and l2.score < config.l2.min_l2_score:
        return None

    return EntrySignal(
        side=side,
        price=float(row["close"]),
        bar_index=bar_index,
        trend_score=trend.score,
        l2_score=l2.score,
        reason=f"pullback_{side.value.lower()}_trend={trend.score:.0f}_l2={l2.score:.0f}",
    )
