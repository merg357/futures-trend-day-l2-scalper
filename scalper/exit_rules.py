"""Exit rules: stop, target, breakeven, trailing, max time, L2 reversal, session end."""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from scalper.config import ExitConfig, ScalperConfig
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


def _update_extremes(pos: Position, bar: pd.Series) -> None:
    pos.highest_price = max(pos.highest_price, float(bar["high"]))
    pos.lowest_price = min(pos.lowest_price, float(bar["low"]))


def _apply_breakeven(pos: Position, bar: pd.Series, cfg: ExitConfig, tick_size: float) -> None:
    if not cfg.breakeven_enabled or pos.breakeven_active:
        return
    trigger = cfg.breakeven_trigger_ticks * tick_size
    offset = cfg.breakeven_offset_ticks * tick_size
    if pos.side == Side.LONG and float(bar["high"]) >= pos.entry_price + trigger:
        pos.stop_price = max(pos.stop_price, pos.entry_price + offset)
        pos.breakeven_active = True
    elif pos.side == Side.SHORT and float(bar["low"]) <= pos.entry_price - trigger:
        pos.stop_price = min(pos.stop_price, pos.entry_price - offset)
        pos.breakeven_active = True


def _apply_trailing(pos: Position, bar: pd.Series, cfg: ExitConfig, tick_size: float) -> None:
    if not cfg.trailing_enabled:
        return
    trigger = cfg.trailing_trigger_ticks * tick_size
    trail = cfg.trailing_offset_ticks * tick_size
    if pos.side == Side.LONG:
        if float(bar["high"]) >= pos.entry_price + trigger:
            pos.trailing_active = True
        if pos.trailing_active:
            pos.stop_price = max(pos.stop_price, pos.highest_price - trail)
    else:
        if float(bar["low"]) <= pos.entry_price - trigger:
            pos.trailing_active = True
        if pos.trailing_active:
            pos.stop_price = min(pos.stop_price, pos.lowest_price + trail)


def _check_stop_target(pos: Position, bar: pd.Series) -> tuple[float | None, ExitReason | None]:
    if pos.side == Side.LONG:
        if float(bar["low"]) <= pos.stop_price:
            reason = ExitReason.TRAILING if pos.trailing_active else (
                ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP
            )
            return pos.stop_price, reason
        if float(bar["high"]) >= pos.target_price:
            return pos.target_price, ExitReason.TARGET
    else:
        if float(bar["high"]) >= pos.stop_price:
            reason = ExitReason.TRAILING if pos.trailing_active else (
                ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP
            )
            return pos.stop_price, reason
        if float(bar["low"]) <= pos.target_price:
            return pos.target_price, ExitReason.TARGET
    return None, None


def _l2_reversal(row: pd.Series, pos: Position, config: ScalperConfig) -> bool:
    if not config.exit.l2_reversal_exit_enabled:
        return False
    l2 = compute_l2_score(row, pos.side, config.l2)
    return l2.score < config.exit.l2_reversal_threshold


def _parse_time(t: str) -> time:
    h, m = t.split(":")
    return time(int(h), int(m))


def is_session_end(bar_time: datetime, config: ScalperConfig) -> bool:
    if not config.exit.exit_at_session_end:
        return False
    close_t = _parse_time(config.session.rth_close)
    flatten_min = config.session.flatten_before_close_minutes
    bar_t = bar_time.time()
    close_minutes = close_t.hour * 60 + close_t.minute - flatten_min
    bar_minutes = bar_t.hour * 60 + bar_t.minute
    return bar_minutes >= close_minutes


def evaluate_exit(
    pos: Position,
    bar: pd.Series,
    bar_index: int,
    bar_time: datetime,
    config: ScalperConfig,
) -> tuple[float | None, ExitReason | None]:
    _update_extremes(pos, bar)
    _apply_breakeven(pos, bar, config.exit, config.tick_size)
    _apply_trailing(pos, bar, config.exit, config.tick_size)

    price, reason = _check_stop_target(pos, bar)
    if price is not None:
        return price, reason

    if bar_index - pos.entry_bar >= config.exit.max_hold_bars:
        return float(bar["close"]), ExitReason.MAX_TIME

    if _l2_reversal(bar, pos, config):
        return float(bar["close"]), ExitReason.L2_REVERSAL

    if is_session_end(bar_time, config):
        return float(bar["close"]), ExitReason.SESSION_END

    return None, None
