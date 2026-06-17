"""Multi-timeframe backtest: trend/chop on higher TF, execution on 1m."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from scalper.backtest import (
    _apply_slippage,
    _calc_pnl,
    _compute_metrics,
    load_bars,
)
from scalper.config import ScalperConfig, load_config
from scalper.entry_rules import is_chop
from scalper.exit_rules import evaluate_exit, init_position
from scalper.indicators import compute_indicators
from scalper.l2_score import compute_l2_score
from scalper.models import BacktestResult, Bias, ExitReason, Side, Trade
from scalper.risk import RiskManager
from scalper.trend_score import compute_trend_score

MTF_TREND_COLS = (
    "ema_fast",
    "ema_slow",
    "ema_trend",
    "vwap",
    "atr",
    "adx",
    "higher_high",
    "lower_low",
    "bar_range",
)


def _resample_bars(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate OHLCV (+ L2 if present) to N-minute bars."""
    ts = df.set_index("timestamp")
    agg: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    for col in ("bid", "ask", "bid_size", "ask_size", "bid_depth", "ask_depth", "delta"):
        if col in ts.columns:
            agg[col] = "last"
    rule = f"{minutes}min"
    out = ts.resample(rule, label="left", closed="left").agg(agg)
    out = out.dropna(subset=["open"]).reset_index()
    return out


def prepare_mtf_dataframe(
    df_1m: pd.DataFrame,
    config: ScalperConfig,
    trend_minutes: int = 5,
) -> pd.DataFrame:
    """Compute 1m execution bars with 5m trend indicators forward-filled."""
    df = compute_indicators(df_1m.copy(), config.trend)
    df_5m = _resample_bars(df_1m, trend_minutes)
    df_5m = compute_indicators(df_5m, config.trend)

    df_sorted = df.sort_values("timestamp").reset_index(drop=True)
    htf = df_5m.sort_values("timestamp")[
        ["timestamp", *MTF_TREND_COLS]
    ].rename(columns={c: f"mtf_{c}" for c in MTF_TREND_COLS})

    merged = pd.merge_asof(
        df_sorted,
        htf,
        on="timestamp",
        direction="backward",
    )
    return merged


def _trend_row_from_mtf(row: pd.Series) -> pd.Series:
    """Build a synthetic row using higher-TF indicators for trend/chop."""
    work = row.copy()
    for col in MTF_TREND_COLS:
        mtf_col = f"mtf_{col}"
        if mtf_col in row.index and pd.notna(row.get(mtf_col)):
            work[col] = row[mtf_col]
    return work


def _pullback_to_ema(row: pd.Series, bias: Bias, tick_size: float, tolerance_ticks: int) -> bool:
    tol = tolerance_ticks * tick_size
    ema_fast = row.get("mtf_ema_fast", row.get("ema_fast"))
    if bias == Bias.LONG:
        return row["low"] <= ema_fast + tol and row["close"] > ema_fast
    if bias == Bias.SHORT:
        return row["high"] >= ema_fast - tol and row["close"] < ema_fast
    return False


def evaluate_entry_mtf(
    row: pd.Series,
    prev_atr: float,
    bar_index: int,
    config: ScalperConfig,
    cooldown_remaining: int,
    session_bar_index: int,
    bar_time: datetime,
) -> object | None:
    from scalper.models import EntrySignal
    from scalper.session_utils import rth_entry_block_reason

    if cooldown_remaining > 0:
        return None
    if rth_entry_block_reason(bar_time, config) is not None:
        return None
    if session_bar_index < config.entry.min_bars_after_open:
        return None

    trend_row = _trend_row_from_mtf(row)
    if is_chop(trend_row, config.entry):
        return None

    bid = row.get("bid")
    ask = row.get("ask")
    if pd.notna(bid) and pd.notna(ask) and config.entry.max_spread_ticks > 0:
        spread_ticks = (float(ask) - float(bid)) / config.tick_size
        if spread_ticks > config.entry.max_spread_ticks:
            return None

    trend = compute_trend_score(trend_row, prev_atr, config.trend)
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
        reason=f"mtf_pullback_{side.value.lower()}_trend={trend.score:.0f}_l2={l2.score:.0f}",
    )


def run_mtf_backtest(
    config: ScalperConfig,
    data_path: str | Path,
    config_path: str = "",
    trend_minutes: int = 5,
) -> BacktestResult:
    """Backtest with trend/chop on higher timeframe and execution on 1m bars."""
    df = prepare_mtf_dataframe(load_bars(data_path), config, trend_minutes=trend_minutes)

    l2_cols = {"bid_size", "ask_size", "bid_depth", "ask_depth"}
    has_l2 = l2_cols.issubset(df.columns) and df[list(l2_cols)].notna().any().any()
    warnings: list[str] = [f"MTF mode: trend/chop on {trend_minutes}m, execution on 1m."]
    l2_approximated = False
    if not has_l2:
        l2_approximated = True
        warnings.append("L2 columns missing or empty — using OHLCV approximation mode for L2 scoring.")

    risk = RiskManager(config)
    trades: list[Trade] = []
    position = None
    cooldown = 0
    session_bar = 0
    equity = [config.backtest.initial_capital]
    commission_total = 0.0
    entry_trend = 0.0
    entry_l2 = 0.0
    prev_session_date = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]
        if isinstance(ts, str):
            ts = pd.to_datetime(ts)
        session_date = ts.date() if hasattr(ts, "date") else None
        if session_date != prev_session_date:
            risk.reset_session()
            session_bar = 0
            prev_session_date = session_date
        session_bar += 1

        if cooldown > 0:
            cooldown -= 1

        if position is not None:
            exit_price, reason = evaluate_exit(position, row, i, ts, config)
            if exit_price is not None and reason is not None:
                fill = _apply_slippage(
                    exit_price, position.side, False, config.backtest.slippage_ticks, config.tick_size
                )
                comm = config.backtest.commission_per_side * position.quantity * 2
                commission_total += comm
                pnl, pnl_ticks = _calc_pnl(
                    position.side,
                    position.entry_price,
                    fill,
                    position.quantity,
                    config.tick_size,
                    config.tick_value,
                )
                pnl -= comm
                trade = Trade(
                    side=position.side,
                    entry_time=position.entry_time,
                    exit_time=ts,
                    entry_price=position.entry_price,
                    exit_price=fill,
                    quantity=position.quantity,
                    pnl=pnl,
                    pnl_ticks=pnl_ticks,
                    commission=comm,
                    exit_reason=reason,
                    bars_held=i - position.entry_bar,
                    entry_trend_score=entry_trend,
                    entry_l2_score=entry_l2,
                )
                trades.append(trade)
                risk.record_trade(trade)
                equity.append(equity[-1] + pnl)
                position = None
                cooldown = config.entry.cooldown_bars_after_exit
            continue

        if not risk.can_enter():
            equity.append(equity[-1])
            continue

        prev_atr = float(prev.get("mtf_atr", prev.get("atr", 0)))
        signal = evaluate_entry_mtf(row, prev_atr, i, config, cooldown, session_bar, ts)
        if signal is None:
            equity.append(equity[-1])
            continue

        fill = _apply_slippage(signal.price, signal.side, True, config.backtest.slippage_ticks, config.tick_size)
        qty = risk.position_size()
        position = init_position(signal.side, fill, i, ts, qty, config)
        entry_trend = signal.trend_score
        entry_l2 = signal.l2_score
        equity.append(equity[-1])

    if position is not None:
        last = df.iloc[-1]
        ts = last["timestamp"]
        fill = float(last["close"])
        comm = config.backtest.commission_per_side * position.quantity * 2
        commission_total += comm
        pnl, pnl_ticks = _calc_pnl(
            position.side,
            position.entry_price,
            fill,
            position.quantity,
            config.tick_size,
            config.tick_value,
        )
        pnl -= comm
        trades.append(
            Trade(
                side=position.side,
                entry_time=position.entry_time,
                exit_time=ts,
                entry_price=position.entry_price,
                exit_price=fill,
                quantity=position.quantity,
                pnl=pnl,
                pnl_ticks=pnl_ticks,
                commission=comm,
                exit_reason=ExitReason.SESSION_END,
                bars_held=len(df) - 1 - position.entry_bar,
                entry_trend_score=entry_trend,
                entry_l2_score=entry_l2,
            )
        )
        equity.append(equity[-1] + pnl)

    metrics = _compute_metrics(trades, equity, commission_total)
    trade_dicts = [
        {
            "side": t.side.value,
            "entry_time": str(t.entry_time),
            "exit_time": str(t.exit_time),
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "pnl": t.pnl,
            "pnl_ticks": t.pnl_ticks,
            "commission": t.commission,
            "exit_reason": t.exit_reason.value,
            "bars_held": t.bars_held,
            "entry_trend_score": t.entry_trend_score,
            "entry_l2_score": t.entry_l2_score,
        }
        for t in trades
    ]

    return BacktestResult(
        symbol=config.symbol,
        config_path=config_path,
        data_path=str(data_path),
        start_time=str(df["timestamp"].iloc[0]),
        end_time=str(df["timestamp"].iloc[-1]),
        bars_processed=len(df),
        trades=trade_dicts,
        metrics=metrics,
        equity_curve=equity,
        warnings=warnings,
        l2_approximated=l2_approximated,
    )


def run_mtf_backtest_from_paths(
    config_path: str | Path,
    data_path: str | Path,
    trend_minutes: int = 5,
) -> BacktestResult:
    config = load_config(config_path)
    return run_mtf_backtest(config, data_path, config_path=str(config_path), trend_minutes=trend_minutes)
