"""Event-driven backtester."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from scalper.config import ScalperConfig, load_config
from scalper.entry_rules import evaluate_entry
from scalper.exit_rules import evaluate_exit, init_position
from scalper.indicators import compute_indicators
from scalper.models import BacktestMetrics, BacktestResult, ExitReason, Side, Trade
from scalper.risk import RiskManager


def load_bars(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    return df.sort_values("timestamp").reset_index(drop=True)


def _apply_slippage(price: float, side: Side, is_entry: bool, slippage_ticks: int, tick_size: float) -> float:
    slip = slippage_ticks * tick_size
    if is_entry:
        return price + slip if side == Side.LONG else price - slip
    return price - slip if side == Side.LONG else price + slip


def _calc_pnl(side: Side, entry: float, exit_p: float, qty: int, tick_size: float, tick_value: float) -> tuple[float, float]:
    ticks = (exit_p - entry) / tick_size
    if side == Side.SHORT:
        ticks = -ticks
    pnl = ticks * tick_value * qty
    return pnl, ticks


def _compute_metrics(trades: list[Trade], equity: list[float], commission_total: float) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(total_commission=commission_total)

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross = sum(pnls)
    net = gross - commission_total
    win_rate = len(wins) / len(trades) if trades else 0.0
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    eq = np.array(equity)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    max_dd = float(dd.max()) if len(dd) else 0.0

    returns = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([0.0])
    sharpe = float(returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0

    return BacktestMetrics(
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=win_rate,
        gross_pnl=gross,
        net_pnl=net,
        total_commission=commission_total,
        profit_factor=pf if pf != float("inf") else 999.0,
        avg_win=float(np.mean(wins)) if wins else 0.0,
        avg_loss=float(np.mean(losses)) if losses else 0.0,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        avg_bars_held=float(np.mean([t.bars_held for t in trades])),
    )


def run_backtest(
    config: ScalperConfig,
    data_path: str | Path,
    config_path: str = "",
) -> BacktestResult:
    df = load_bars(data_path)
    df = compute_indicators(df, config.trend)

    l2_cols = {"bid_size", "ask_size", "bid_depth", "ask_depth"}
    has_l2 = l2_cols.issubset(df.columns) and df[list(l2_cols)].notna().any().any()
    warnings: list[str] = []
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
                fill = _apply_slippage(exit_price, position.side, False, config.backtest.slippage_ticks, config.tick_size)
                comm = config.backtest.commission_per_side * position.quantity * 2
                commission_total += comm
                pnl, pnl_ticks = _calc_pnl(
                    position.side, position.entry_price, fill,
                    position.quantity, config.tick_size, config.tick_value,
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

        signal = evaluate_entry(row, float(prev.get("atr", 0)), i, config, cooldown, session_bar)
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
            position.side, position.entry_price, fill,
            position.quantity, config.tick_size, config.tick_value,
        )
        pnl -= comm
        trades.append(Trade(
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
        ))
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


def run_backtest_from_paths(config_path: str | Path, data_path: str | Path) -> BacktestResult:
    config = load_config(config_path)
    return run_backtest(config, data_path, config_path=str(config_path))
