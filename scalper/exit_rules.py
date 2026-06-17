"""Exit rules: stop, target, breakeven, trailing, max time, L2 reversal, session end."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from scalper.config import ExitConfig, FlowConfig, ScalperConfig
from scalper.entry_rules import _is_synthetic_flat_bar
from scalper.session_utils import is_session_end
from scalper.l2_score import compute_l2_score
from scalper.models import ExitReason, Position, Side


def _num(row: pd.Series, key: str, default: float = 0.0) -> float:
    val = row.get(key, default)
    if pd.isna(val):
        return default
    return float(val)


def flow_exit_delta_flip(row: pd.Series, side: Side, flow: FlowConfig) -> bool:
    """Exit when intrabar delta crosses opposite-side entry threshold."""
    delta = _num(row, "delta")
    if side == Side.LONG:
        return delta <= flow.short_delta_max
    return delta >= flow.long_delta_min


def flow_exit_mbo_reversal(row: pd.Series, side: Side, flow: FlowConfig) -> bool:
    """Exit when MBO new-order skew flips against the position."""
    bid_new = _num(row, "mbo_bid_new_count")
    ask_new = _num(row, "mbo_ask_new_count")
    ratio = flow.book_size_ratio
    if side == Side.LONG:
        if bid_new <= 0:
            return ask_new > 0
        return ask_new / bid_new >= ratio
    if ask_new <= 0:
        return bid_new > 0
    return bid_new / ask_new >= ratio


def adverse_entry_mid_moved_against(
    mid: float,
    limit_price: float,
    side: Side,
    tick_size: float,
    adverse_ticks: int,
) -> bool:
    if mid <= 0 or limit_price <= 0:
        return False
    threshold = adverse_ticks * tick_size
    if side == Side.LONG:
        return mid <= limit_price - threshold
    return mid >= limit_price + threshold


def _ticks_to_price(ticks: int, tick_size: float, base: float, side: Side, favorable: bool) -> float:
    offset = ticks * tick_size
    if side == Side.LONG:
        return base + offset if favorable else base - offset
    return base - offset if favorable else base + offset


def init_position(
    side: Side,
    entry_price: float,
    entry_bar: int,
    entry_time: datetime,
    quantity: int,
    config: ScalperConfig,
) -> Position:
    stop = _ticks_to_price(config.exit.stop_loss_ticks, config.tick_size, entry_price, side, favorable=False)
    use_tp = config.filters.use_take_profit and config.exit.take_profit_ticks > 0
    if use_tp:
        target = _ticks_to_price(config.exit.take_profit_ticks, config.tick_size, entry_price, side, favorable=True)
    else:
        # Unreachable target when TP disabled (MES raw test).
        huge = entry_price + 10000 * config.tick_size if side == Side.LONG else entry_price - 10000 * config.tick_size
        target = huge
    return Position(
        side=side,
        entry_price=entry_price,
        entry_bar=entry_bar,
        entry_time=entry_time,
        quantity=quantity,
        stop_price=stop,
        target_price=target,
        highest_price=entry_price,
        lowest_price=entry_price,
    )


def stop_is_correct_side_of_entry(side: Side, entry_price: float, stop_price: float) -> bool:
    """Long stop must be below entry; short stop must be above entry."""
    if entry_price <= 0 or stop_price <= 0:
        return False
    if side == Side.LONG:
        return stop_price < entry_price
    return stop_price > entry_price


def stop_side_metadata(
    side: Side,
    entry_price: float,
    stop_price: float,
    tick_size: float,
) -> dict[str, object]:
    """Logging payload for stop placement verification."""
    distance_ticks = abs(entry_price - stop_price) / tick_size if tick_size > 0 else 0.0
    return {
        "entry_side": side.value,
        "entry_price": entry_price,
        "initial_stop_price": stop_price,
        "initial_stop_distance_ticks": round(distance_ticks, 2),
        "stop_is_correct_side_of_entry": stop_is_correct_side_of_entry(side, entry_price, stop_price),
    }


def _effective_high_low(bar: pd.Series) -> tuple[float, float]:
    """On synthetic flat OHLC bars, merge bid/ask into extremes for exit checks."""
    high = float(bar["high"])
    low = float(bar["low"])
    if not _is_synthetic_flat_bar(bar):
        return high, low
    close = float(bar["close"])
    eff_high, eff_low = high, low
    ask = bar.get("ask")
    bid = bar.get("bid")
    if pd.notna(ask):
        eff_high = max(eff_high, close, float(ask))
    if pd.notna(bid):
        eff_low = min(eff_low, close, float(bid))
    return eff_high, eff_low


def _update_extremes(pos: Position, eff_high: float, eff_low: float) -> None:
    pos.highest_price = max(pos.highest_price, eff_high)
    pos.lowest_price = min(pos.lowest_price, eff_low)


def _apply_breakeven(
    pos: Position, eff_high: float, eff_low: float, cfg: ExitConfig, tick_size: float,
) -> None:
    if not cfg.breakeven_enabled or pos.breakeven_active:
        return
    trigger = cfg.breakeven_trigger_ticks * tick_size
    offset = cfg.breakeven_offset_ticks * tick_size
    if pos.side == Side.LONG and eff_high >= pos.entry_price + trigger:
        pos.stop_price = max(pos.stop_price, pos.entry_price + offset)
        pos.breakeven_active = True
    elif pos.side == Side.SHORT and eff_low <= pos.entry_price - trigger:
        pos.stop_price = min(pos.stop_price, pos.entry_price - offset)
        pos.breakeven_active = True


def _apply_trailing(
    pos: Position, eff_high: float, eff_low: float, cfg: ExitConfig, tick_size: float,
) -> None:
    if not cfg.trailing_enabled:
        return
    trigger = cfg.trailing_trigger_ticks * tick_size
    trail = cfg.trailing_offset_ticks * tick_size
    step = cfg.trailing_step_ticks * tick_size if cfg.trailing_step_ticks > 0 else 0.0
    if pos.side == Side.LONG:
        if eff_high >= pos.entry_price + trigger:
            pos.trailing_active = True
        if pos.trailing_active:
            candidate = pos.highest_price - trail
            if step > 0:
                if candidate > pos.stop_price + step:
                    pos.stop_price = pos.stop_price + step
            else:
                pos.stop_price = max(pos.stop_price, candidate)
    else:
        if eff_low <= pos.entry_price - trigger:
            pos.trailing_active = True
        if pos.trailing_active:
            candidate = pos.lowest_price + trail
            if step > 0:
                if candidate < pos.stop_price - step:
                    pos.stop_price = pos.stop_price - step
            else:
                pos.stop_price = min(pos.stop_price, candidate)


def _check_stop_target(
    pos: Position, eff_high: float, eff_low: float, config: ScalperConfig,
) -> tuple[float | None, ExitReason | None]:
    use_tp = config.filters.use_take_profit and config.exit.take_profit_ticks > 0
    if pos.side == Side.LONG:
        if eff_low <= pos.stop_price:
            reason = ExitReason.TRAILING if pos.trailing_active else (
                ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP
            )
            return pos.stop_price, reason
        if use_tp and eff_high >= pos.target_price:
            return pos.target_price, ExitReason.TARGET
    else:
        if eff_high >= pos.stop_price:
            reason = ExitReason.TRAILING if pos.trailing_active else (
                ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP
            )
            return pos.stop_price, reason
        if use_tp and eff_low <= pos.target_price:
            return pos.target_price, ExitReason.TARGET
    return None, None


def _l2_reversal(row: pd.Series, pos: Position, config: ScalperConfig) -> bool:
    if not config.exit.l2_reversal_exit_enabled:
        return False
    l2 = compute_l2_score(row, pos.side, config.l2)
    return l2.score < config.exit.l2_reversal_threshold


def evaluate_flow_exit(
    pos: Position,
    snap: pd.Series,
    config: ScalperConfig,
) -> tuple[float | None, ExitReason | None]:
    """Intrabar flow-based exit using live orderflow snapshot (mid + MBO + delta)."""
    mid = float(snap.get("close") or snap.get("bid") or 0)
    if mid <= 0:
        return None, None

    eff_high = max(pos.highest_price, mid)
    eff_low = min(pos.lowest_price, mid)
    _update_extremes(pos, eff_high, eff_low)
    _apply_breakeven(pos, eff_high, eff_low, config.exit, config.tick_size)
    _apply_trailing(pos, eff_high, eff_low, config.exit, config.tick_size)

    if not config.filters.use_signal_flip_exit:
        pass
    elif flow_exit_delta_flip(snap, pos.side, config.flow):
        return mid, ExitReason.FLOW_DELTA_FLIP
    elif flow_exit_mbo_reversal(snap, pos.side, config.flow):
        return mid, ExitReason.FLOW_MBO_REVERSAL

    if pos.trailing_active or pos.breakeven_active:
        if pos.side == Side.LONG and mid <= pos.stop_price:
            return mid, ExitReason.FLOW_TRAIL_INTRABAR
        if pos.side == Side.SHORT and mid >= pos.stop_price:
            return mid, ExitReason.FLOW_TRAIL_INTRABAR

    return None, None


def evaluate_exit(
    pos: Position,
    bar: pd.Series,
    bar_index: int,
    bar_time: datetime,
    config: ScalperConfig,
) -> tuple[float | None, ExitReason | None]:
    eff_high, eff_low = _effective_high_low(bar)
    _update_extremes(pos, eff_high, eff_low)
    _apply_breakeven(pos, eff_high, eff_low, config.exit, config.tick_size)
    _apply_trailing(pos, eff_high, eff_low, config.exit, config.tick_size)

    price, reason = _check_stop_target(pos, eff_high, eff_low, config)
    if price is not None:
        return price, reason

    max_hold = config.exit.max_hold_bars
    if config.filters.use_max_hold_time and max_hold > 0 and bar_index - pos.entry_bar >= max_hold:
        return float(bar["close"]), ExitReason.MAX_TIME

    if _l2_reversal(bar, pos, config):
        return float(bar["close"]), ExitReason.L2_REVERSAL

    if is_session_end(bar_time, config):
        return float(bar["close"]), ExitReason.SESSION_END

    return None, None
