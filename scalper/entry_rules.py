"""Entry rules: chop filter, EMA pullback entries, and intrabar flow-burst entries."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from scalper.config import EntryConfig, ScalperConfig
from scalper.flow_signals import (
    FlowSignal,
    compute_flow_for_side,
    compute_flow_signal,
    flow_burst_passes,
    flow_supports_side,
)
from scalper.nq_confirmation import nq_veto_comparison, nq_veto_reason
from scalper.session_utils import rth_entry_block_reason
from scalper.l2_score import compute_l2_score
from scalper.models import Bias, EntrySignal, Side
from scalper.trend_score import compute_trend_score


def _is_synthetic_flat_bar(row: pd.Series) -> bool:
    """Skip range chop on bridge flat OHLC or zero-range zero-volume bars."""
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    if o == h == l == c:
        return True
    bar_range = row.get("bar_range", h - l)
    vol = row.get("volume", 0)
    if (vol == 0 or pd.isna(vol)) and (bar_range == 0 or pd.isna(bar_range)):
        return True
    return False


def is_chop(row: pd.Series, cfg: EntryConfig) -> bool:
    if not cfg.chop_filter_enabled:
        return False
    adx_val = row.get("adx", 0.0)
    if pd.notna(adx_val) and adx_val <= cfg.chop_adx_max:
        return True
    if _is_synthetic_flat_bar(row):
        return False
    atr_val = row.get("atr", 1.0)
    bar_range = row.get("bar_range", row["high"] - row["low"])
    if pd.notna(atr_val) and atr_val > 0 and bar_range < atr_val * cfg.chop_range_atr_mult:
        return True
    return False


def _effective_high_low(row: pd.Series) -> tuple[float, float]:
    """On flat OHLC bars, merge bid/ask into bar extremes for pullback checks."""
    high = float(row["high"])
    low = float(row["low"])
    if not _is_synthetic_flat_bar(row):
        return high, low
    close = float(row["close"])
    eff_high, eff_low = high, low
    ask = row.get("ask")
    bid = row.get("bid")
    if pd.notna(ask):
        eff_high = max(eff_high, close, float(ask))
    if pd.notna(bid):
        eff_low = min(eff_low, close, float(bid))
    return eff_high, eff_low


def _pullback_to_ema(row: pd.Series, bias: Bias, tick_size: float, tolerance_ticks: int) -> bool:
    tol = tolerance_ticks * tick_size
    eff_high, eff_low = _effective_high_low(row)
    flat = _is_synthetic_flat_bar(row)
    if bias == Bias.LONG:
        touch = eff_low <= row["ema_fast"] + tol
        reclaim = float(row["close"]) > row["ema_fast"]
        if flat and pd.notna(row.get("ask")):
            reclaim = max(float(row["close"]), float(row["ask"])) > row["ema_fast"]
        return touch and reclaim
    if bias == Bias.SHORT:
        touch = eff_high >= row["ema_fast"] - tol
        reclaim = float(row["close"]) < row["ema_fast"]
        if flat and pd.notna(row.get("bid")):
            reclaim = min(float(row["close"]), float(row["bid"])) < row["ema_fast"]
        return touch and reclaim
    return False


def _flat_strong_flow_bias(
    row: pd.Series,
    flow_any: FlowSignal | None,
    cfg: ScalperConfig,
) -> Bias | None:
    """On synthetic flat OHLC, strong directional flow substitutes for trend bias."""
    if not _is_synthetic_flat_bar(row):
        return None
    if flow_any is None or flow_any.side is None:
        return None
    if flow_any.score < cfg.flow.min_flow_score:
        return None
    if flow_any.triggers_hit < cfg.flow.min_triggers:
        return None
    return Bias.LONG if flow_any.side == Side.LONG else Bias.SHORT


def _entry_guards(
    row: pd.Series,
    config: ScalperConfig,
    cooldown_remaining: int,
    session_bar_index: int,
    bar_time: datetime,
) -> str | None:
    """Shared pre-checks for pullback and flow-burst paths. Returns block reason or None."""
    flags = config.filters
    if flags.use_cooldown and cooldown_remaining > 0:
        return "cooldown"
    if flags.use_session_filter and rth_entry_block_reason(bar_time, config) is not None:
        return "rth_block"
    if flags.use_time_filter and session_bar_index < config.entry.min_bars_after_open:
        return "session_warmup"
    if is_chop(row, config.entry):
        return "chop"
    if not config.is_mes_es_nq_mode():
        bid = row.get("bid")
        ask = row.get("ask")
        max_spread = config.entry.max_spread_ticks
        if pd.notna(bid) and pd.notna(ask) and max_spread > 0:
            spread_ticks = (float(ask) - float(bid)) / config.tick_size
            if spread_ticks > max_spread:
                return "spread"
    return None


def evaluate_flow_burst_entry(
    row: pd.Series,
    prev_atr: float,
    bar_index: int,
    config: ScalperConfig,
    cooldown_remaining: int,
    session_bar_index: int,
    bar_time: datetime,
    prev_row: pd.Series | None = None,
    *,
    trend_row: pd.Series | None = None,
) -> EntrySignal | None:
    """Momentum burst entry — skips EMA pullback (research moves were not pullbacks).

    Flow source: L2/DOM proxy via orderflow.json or CSV bridge columns — not live MBO parquet.
    """
    if not config.entry.flow_burst_mode or not config.entry.use_flow_signals:
        return None

    blocked = _entry_guards(row, config, cooldown_remaining, session_bar_index, bar_time)
    if blocked:
        return None

    flow_any = compute_flow_signal(row, prev_row, config.flow)
    if flow_any.side is None:
        return None
    if not flow_burst_passes(flow_any, flow_any.side, config.flow):
        return None

    side = flow_any.side
    trend_source = trend_row if trend_row is not None else row
    trend = compute_trend_score(trend_source, prev_atr, config.trend)
    trend_aligns = (
        (side == Side.LONG and trend.bias == Bias.LONG)
        or (side == Side.SHORT and trend.bias == Bias.SHORT)
    )
    if not trend_aligns and flow_any.score < config.flow.flow_strong_score:
        return None

    if config.entry.pullback_required_for_burst:
        bias = Bias.LONG if side == Side.LONG else Bias.SHORT
        if not _pullback_to_ema(row, bias, config.tick_size, config.entry.pullback_to_ema_ticks):
            return None

    l2 = compute_l2_score(row, side, config.l2)
    l2_ok = l2.score >= config.l2.min_l2_score
    flow = compute_flow_for_side(row, prev_row, side, config.flow)
    flow_ok = flow_burst_passes(flow, side, config.flow)
    if config.entry.require_l2_confirmation and not l2_ok and not flow_ok:
        return None

    veto_cmp = nq_veto_comparison(side, config)
    if veto_cmp.get("nq_veto_reason"):
        return None

    return EntrySignal(
        side=side,
        price=float(row["close"]),
        bar_index=bar_index,
        trend_score=trend.score,
        l2_score=l2.score,
        reason=(
            f"flow_burst_{side.value.lower()}"
            f"_flow={flow.score:.0f}t{flow.triggers_hit}"
            f"_l2={l2.score:.0f}"
        ),
    )


def evaluate_entry(
    row: pd.Series,
    prev_atr: float,
    bar_index: int,
    config: ScalperConfig,
    cooldown_remaining: int,
    session_bar_index: int,
    bar_time: datetime,
    prev_row: pd.Series | None = None,
) -> EntrySignal | None:
    """EMA pullback entry path (secondary when flow_burst_mode is primary)."""
    if not config.entry.pullback_mode:
        return None

    blocked = _entry_guards(row, config, cooldown_remaining, session_bar_index, bar_time)
    if blocked:
        return None

    flow_any = None
    if config.entry.use_flow_signals:
        flow_any = compute_flow_signal(row, prev_row, config.flow)

    min_trend = config.trend.min_trend_score
    if flow_any is not None and flow_any.score >= config.flow.flow_strong_score:
        min_trend = min(min_trend, config.flow.relaxed_min_trend_score)

    trend = compute_trend_score(row, prev_atr, config.trend)
    flow_bias = (
        _flat_strong_flow_bias(row, flow_any, config)
        if config.entry.use_flow_signals
        else None
    )
    effective_bias = flow_bias if flow_bias is not None else trend.bias
    if effective_bias == Bias.NONE:
        return None
    if flow_bias is None and trend.score < min_trend:
        return None
    if not _pullback_to_ema(row, effective_bias, config.tick_size, config.entry.pullback_to_ema_ticks):
        return None

    side = Side.LONG if effective_bias == Bias.LONG else Side.SHORT
    l2 = compute_l2_score(row, side, config.l2)
    l2_ok = l2.score >= config.l2.min_l2_score
    flow = (
        compute_flow_for_side(row, prev_row, side, config.flow)
        if config.entry.use_flow_signals
        else None
    )
    flow_ok = flow is not None and flow_supports_side(flow, side, config.flow)
    if config.entry.require_l2_confirmation and not l2_ok:
        if not (config.entry.use_flow_signals and flow_ok):
            return None

    veto = nq_veto_reason(side, config)
    if veto:
        return None

    flow_part = ""
    if flow is not None and flow_ok:
        flow_part = f"_flow={flow.score:.0f}t{flow.triggers_hit}"
    bias_tag = "flow_bias" if flow_bias is not None else f"trend={trend.score:.0f}"
    return EntrySignal(
        side=side,
        price=float(row["close"]),
        bar_index=bar_index,
        trend_score=trend.score,
        l2_score=l2.score,
        reason=(
            f"pullback_{side.value.lower()}_{bias_tag}"
            f"_l2={l2.score:.0f}{flow_part}"
        ),
    )
