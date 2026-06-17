"""Exit rules: stop, target, breakeven, trailing, max time, L2 reversal, session end."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from scalper.config import ExitConfig, ScalperConfig
from scalper.entry_rules import _is_synthetic_flat_bar
from scalper.session_utils import is_session_end
from scalper.l2_score import compute_l2_score
from scalper.models import ExitReason, Position, Side


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
    target = _ticks_to_price(config.exit.take_profit_ticks, config.tick_size, entry_price, side, favorable=True)
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
    if pos.side == Side.LONG:
        if eff_high >= pos.entry_price + trigger:
            pos.trailing_active = True
        if pos.trailing_active:
            pos.stop_price = max(pos.stop_price, pos.highest_price - trail)
    else:
        if eff_low <= pos.entry_price - trigger:
            pos.trailing_active = True
        if pos.trailing_active:
            pos.stop_price = min(pos.stop_price, pos.lowest_price + trail)


def _check_stop_target(
    pos: Position, eff_high: float, eff_low: float,
) -> tuple[float | None, ExitReason | None]:
    if pos.side == Side.LONG:
        if eff_low <= pos.stop_price:
            reason = ExitReason.TRAILING if pos.trailing_active else (
                ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP
            )
            return pos.stop_price, reason
        if eff_high >= pos.target_price:
            return pos.target_price, ExitReason.TARGET
    else:
        if eff_high >= pos.stop_price:
            reason = ExitReason.TRAILING if pos.trailing_active else (
                ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP
            )
            return pos.stop_price, reason
        if eff_low <= pos.target_price:
            return pos.target_price, ExitReason.TARGET
    return None, None


def _l2_reversal(row: pd.Series, pos: Position, config: ScalperConfig) -> bool:
    if not config.exit.l2_reversal_exit_enabled:
        return False
    l2 = compute_l2_score(row, pos.side, config.l2)
    return l2.score < config.exit.l2_reversal_threshold


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

    price, reason = _check_stop_target(pos, eff_high, eff_low)
    if price is not None:
        return price, reason

    max_hold = config.exit.max_hold_bars
    if max_hold > 0 and bar_index - pos.entry_bar >= max_hold:
        return float(bar["close"]), ExitReason.MAX_TIME

    if _l2_reversal(bar, pos, config):
        return float(bar["close"]), ExitReason.L2_REVERSAL

    if is_session_end(bar_time, config):
        return float(bar["close"]), ExitReason.SESSION_END

    return None, None
