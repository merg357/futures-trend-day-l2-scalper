"""Paper/replay runner — evaluates signals from CSV bars, never sends live orders."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from scalper.backtest import _apply_slippage, _calc_pnl, load_bars
from scalper.config import ScalperConfig, load_config
from scalper.entry_rules import evaluate_entry
from scalper.exit_rules import evaluate_exit, init_position
from scalper.indicators import compute_indicators
from scalper.live_gateway import LiveGateway, OrderAction, OrderRequest
from scalper.models import ExitReason, Position, Side, Trade
from scalper.risk import RiskManager
from scalper.trading_safety import live_trading_enabled, paper_only_mode

logger = logging.getLogger(__name__)


@dataclass
class RunnerState:
    position: Position | None = None
    cooldown: int = 0
    session_bar: int = 0
    prev_session_date: Any = None
    entry_trend: float = 0.0
    entry_l2: float = 0.0
    bar_index: int = 0
    processed_timestamps: set[str] | None = None


def _env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default))


def resolve_bar_csv_path(explicit: str = "") -> Path:
    """Resolve live bar CSV from CLI, BAR_CSV_PATH, or NT8_EXPORT_PATH."""
    if explicit.strip():
        return Path(explicit.strip())
    for name in ("BAR_CSV_PATH", "NT8_EXPORT_PATH"):
        value = os.getenv(name, "").strip()
        if value:
            return Path(value)
    return Path("data/live/nt8_mnq_1m.csv")


def _bar_csv_missing_message() -> str:
    return (
        "BAR_CSV_PATH (or NT8_EXPORT_PATH) is required.\n"
        "Live L2 must come from NinjaTrader 8, not futuresbot recorder.\n"
        "1) Copy integrations/ninjatrader8/ScalperL2Exporter.cs to "
        "Documents\\NinjaTrader 8\\bin\\Custom\\Strategies\n"
        "2) Compile in NT8 (F5) and enable on an MNQ 1-minute chart (Rithmic connected)\n"
        "3) Set ExportPath / BAR_CSV_PATH to the same CSV (default: data/live/nt8_mnq_1m.csv)\n"
        "See integrations/ninjatrader8/README.md"
    )


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _trade_record(trade: Trade, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "side": trade.side.value,
        "entry_time": str(trade.entry_time),
        "exit_time": str(trade.exit_time),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "quantity": trade.quantity,
        "pnl": trade.pnl,
        "pnl_ticks": trade.pnl_ticks,
        "commission": trade.commission,
        "exit_reason": trade.exit_reason.value,
        "bars_held": trade.bars_held,
        "entry_trend_score": trade.entry_trend_score,
        "entry_l2_score": trade.entry_l2_score,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }


def process_bar(
    row: pd.Series,
    prev: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    risk: RiskManager,
    *,
    mode: str,
    signals_path: Path,
    trades_path: Path,
    gateway: LiveGateway | None = None,
) -> list[Trade]:
    """Process one bar using the same rules as the backtester."""
    new_trades: list[Trade] = []
    i = state.bar_index
    ts = row["timestamp"]
    if isinstance(ts, str):
        ts = pd.to_datetime(ts)

    session_date = ts.date() if hasattr(ts, "date") else None
    if session_date != state.prev_session_date:
        risk.reset_session()
        state.session_bar = 0
        state.prev_session_date = session_date
    state.session_bar += 1

    if state.cooldown > 0:
        state.cooldown -= 1

    if state.position is not None:
        exit_price, reason = evaluate_exit(state.position, row, i, ts, config)
        if exit_price is not None and reason is not None:
            fill = _apply_slippage(
                exit_price, state.position.side, False,
                config.backtest.slippage_ticks, config.tick_size,
            )
            comm = config.backtest.commission_per_side * state.position.quantity * 2
            pnl, pnl_ticks = _calc_pnl(
                state.position.side, state.position.entry_price, fill,
                state.position.quantity, config.tick_size, config.tick_value,
            )
            pnl -= comm
            trade = Trade(
                side=state.position.side,
                entry_time=state.position.entry_time,
                exit_time=ts,
                entry_price=state.position.entry_price,
                exit_price=fill,
                quantity=state.position.quantity,
                pnl=pnl,
                pnl_ticks=pnl_ticks,
                commission=comm,
                exit_reason=reason,
                bars_held=i - state.position.entry_bar,
                entry_trend_score=state.entry_trend,
                entry_l2_score=state.entry_l2,
            )
            new_trades.append(trade)
            risk.record_trade(trade)
            _append_jsonl(trades_path, _trade_record(trade, mode))
            if gateway is not None:
                gateway.submit_order(OrderRequest(
                    action=OrderAction.EXIT,
                    symbol=config.symbol,
                    side=state.position.side,
                    quantity=state.position.quantity,
                    price=fill,
                    reason=reason.value,
                ))
            state.position = None
            state.cooldown = config.entry.cooldown_bars_after_exit
        state.bar_index += 1
        return new_trades

    if not risk.can_enter():
        state.bar_index += 1
        return new_trades

    signal = evaluate_entry(
        row, float(prev.get("atr", 0)), i, config, state.cooldown, state.session_bar,
    )
    if signal is None:
        state.bar_index += 1
        return new_trades

    fill = _apply_slippage(
        signal.price, signal.side, True,
        config.backtest.slippage_ticks, config.tick_size,
    )
    qty = risk.position_size()
    state.position = init_position(signal.side, fill, i, ts, qty, config)
    state.entry_trend = signal.trend_score
    state.entry_l2 = signal.l2_score

    signal_record = {
        "mode": mode,
        "timestamp": str(ts),
        "side": signal.side.value,
        "price": fill,
        "quantity": qty,
        "trend_score": signal.trend_score,
        "l2_score": signal.l2_score,
        "reason": signal.reason,
        "paper_only": paper_only_mode(),
        "live_trading": live_trading_enabled(),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_jsonl(signals_path, signal_record)
    logger.info("Signal: %s %s @ %s (%s)", signal.side.value, config.symbol, fill, signal.reason)

    if gateway is not None:
        gateway.submit_order(OrderRequest(
            action=OrderAction.ENTER,
            symbol=config.symbol,
            side=signal.side,
            quantity=qty,
            price=fill,
            reason=signal.reason,
        ))

    state.bar_index += 1
    return new_trades


def run_replay(
    config: ScalperConfig,
    data_path: Path,
    *,
    log_dir: Path,
    gateway: LiveGateway | None = None,
) -> dict[str, Any]:
    """Replay all bars from a CSV file (paper only)."""
    df = load_bars(data_path)
    df = compute_indicators(df, config.trend)
    risk = RiskManager(config)
    state = RunnerState(processed_timestamps=set())
    signals_path = log_dir / "signals.jsonl"
    trades_path = log_dir / "trades.jsonl"
    all_trades: list[Trade] = []

    for i in range(1, len(df)):
        state.bar_index = i
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        all_trades.extend(process_bar(
            row, prev, state, config, risk,
            mode="replay",
            signals_path=signals_path,
            trades_path=trades_path,
            gateway=gateway,
        ))

    if state.position is not None:
        last = df.iloc[-1]
        ts = last["timestamp"]
        fill = float(last["close"])
        comm = config.backtest.commission_per_side * state.position.quantity * 2
        pnl, pnl_ticks = _calc_pnl(
            state.position.side, state.position.entry_price, fill,
            state.position.quantity, config.tick_size, config.tick_value,
        )
        pnl -= comm
        trade = Trade(
            side=state.position.side,
            entry_time=state.position.entry_time,
            exit_time=ts,
            entry_price=state.position.entry_price,
            exit_price=fill,
            quantity=state.position.quantity,
            pnl=pnl,
            pnl_ticks=pnl_ticks,
            commission=comm,
            exit_reason=ExitReason.SESSION_END,
            bars_held=len(df) - 1 - state.position.entry_bar,
            entry_trend_score=state.entry_trend,
            entry_l2_score=state.entry_l2,
        )
        all_trades.append(trade)
        _append_jsonl(trades_path, _trade_record(trade, "replay"))

    summary = {
        "mode": "replay",
        "symbol": config.symbol,
        "data_path": str(data_path),
        "bars_processed": len(df),
        "signals_log": str(signals_path),
        "trades_log": str(trades_path),
        "trade_count": len(all_trades),
        "paper_only": paper_only_mode(),
        "live_trading": live_trading_enabled(),
    }
    _append_jsonl(log_dir / "runner_events.jsonl", {"event": "replay_complete", **summary})
    return summary


def run_follow(
    config: ScalperConfig,
    data_path: Path,
    *,
    log_dir: Path,
    poll_seconds: float = 2.0,
    warmup_bars: int = 100,
    gateway: LiveGateway | None = None,
) -> None:
    """Follow a growing CSV from NinjaTrader 8 ScalperL2Exporter. Paper only."""
    signals_path = log_dir / "signals.jsonl"
    trades_path = log_dir / "trades.jsonl"
    risk = RiskManager(config)
    state = RunnerState(processed_timestamps=set())
    history = pd.DataFrame()

    logger.info("Following %s (poll=%ss, paper_only=%s)", data_path, poll_seconds, paper_only_mode())

    while True:
        if not data_path.exists():
            logger.warning(
                "Bar file missing: %s — enable ScalperL2Exporter in NT8 (see integrations/ninjatrader8/README.md)",
                data_path,
            )
            time.sleep(poll_seconds)
            continue

        try:
            df = load_bars(data_path)
        except Exception as exc:
            logger.warning("Could not read bars: %s", exc)
            time.sleep(poll_seconds)
            continue

        if len(df) < 2:
            time.sleep(poll_seconds)
            continue

        df = compute_indicators(df, config.trend)
        if history.empty:
            start = max(1, len(df) - warmup_bars)
            history = df.iloc[:start].copy()
            for ts in history["timestamp"].astype(str):
                state.processed_timestamps.add(ts)

        new_rows = df[~df["timestamp"].astype(str).isin(state.processed_timestamps)]
        for i in range(len(new_rows)):
            ts_key = str(new_rows.iloc[i]["timestamp"])
            full_idx = df.index[df["timestamp"].astype(str) == ts_key]
            if len(full_idx) == 0:
                continue
            idx = int(full_idx[0])
            if idx < 1:
                state.processed_timestamps.add(ts_key)
                continue
            row = df.iloc[idx]
            prev = df.iloc[idx - 1]
            state.bar_index = idx
            process_bar(
                row, prev, state, config, risk,
                mode="follow",
                signals_path=signals_path,
                trades_path=trades_path,
                gateway=gateway,
            )
            state.processed_timestamps.add(ts_key)

        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper/replay scalper runner (no live orders by default)")
    parser.add_argument("--config", default=os.getenv("SCALPER_CONFIG", "configs/production/mnq_walkforward_optimized.yaml"))
    parser.add_argument(
        "--data",
        default="",
        help="CSV bar file (replay or follow). Defaults to BAR_CSV_PATH or NT8_EXPORT_PATH",
    )
    parser.add_argument("--mode", choices=["replay", "follow"], default=os.getenv("RUNNER_MODE", "follow"))
    parser.add_argument("--log-dir", default=os.getenv("LIVE_LOG_DIR", "data/live"))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("POLL_SECONDS", "2")))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    gateway = LiveGateway(log_dir=log_dir)
    gateway.connect()

    data_path = resolve_bar_csv_path(args.data)
    if args.mode == "replay" and not args.data.strip():
        raise SystemExit(_bar_csv_missing_message())
    if not args.data and not os.getenv("BAR_CSV_PATH", "").strip() and not os.getenv("NT8_EXPORT_PATH", "").strip():
        logger.warning("BAR_CSV_PATH not set; using default %s", data_path)

    legacy_futuresbot = Path(r"C:\TradeData\futuresbot\live\MNQ_1m_live.csv")
    if data_path == legacy_futuresbot or "futuresbot\\live" in str(data_path).lower():
        logger.warning(
            "BAR_CSV_PATH points at deprecated futuresbot live recorder (%s). "
            "Use NT8 ScalperL2Exporter instead (integrations/ninjatrader8/).",
            data_path,
        )
    if args.mode == "replay":
        summary = run_replay(config, data_path, log_dir=log_dir, gateway=gateway)
        print(json.dumps(summary, indent=2))
    else:
        run_follow(
            config, data_path, log_dir=log_dir,
            poll_seconds=args.poll_seconds, gateway=gateway,
        )


if __name__ == "__main__":
    main()
