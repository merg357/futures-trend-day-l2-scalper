"""Paper/replay runner â€” evaluates signals from CSV bars, never sends live orders."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from scalper.backtest import _apply_slippage, _calc_pnl, load_bars
from scalper.config import ScalperConfig, load_config
from scalper.entry_rules import evaluate_entry, evaluate_flow_burst_entry
from scalper.exit_rules import (
    adverse_entry_mid_moved_against,
    evaluate_exit,
    evaluate_flow_exit,
    flow_exit_delta_flip,
    flow_exit_mbo_reversal,
    init_position,
)
from scalper.flow_signals import build_intrabar_snapshot_row, enrich_bar_from_orderflow
from scalper.indicators import compute_indicators
from scalper.live_gateway import LiveGateway, OrderAction, OrderRequest, is_entry_blocked
from scalper.models import ExitReason, Position, Side, Trade
from scalper.risk import RiskManager
from scalper.trading_safety import demo_nt8_orders_enabled, live_trading_enabled, paper_only_mode

logger = logging.getLogger(__name__)


@dataclass
class PendingEntry:
    order_id: str
    submit_ts: float
    side: Side
    limit_price: float
    quantity: int
    reason: str = ""
    signal_ts: float = 0.0
    es_signal_price: float = 0.0
    mes_bid_at_signal: float | None = None
    mes_ask_at_signal: float | None = None
    mes_bid_at_submit: float | None = None
    mes_ask_at_submit: float | None = None
    mes_mid_at_signal: float | None = None
    entry_order_mode: str = "MARKETABLE_LIMIT"


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
    # Intrabar flow burst (L2/DOM + MBO counters via orderflow.json).
    last_flow_burst_poll_ts: float = 0.0
    last_flow_exit_poll_ts: float = 0.0
    last_orderflow_poll_ts: float = 0.0
    last_fast_monitor_ts: float = 0.0
    last_pending_fill_poll_ts: float = 0.0
    last_entry_submit_ts: float = 0.0
    last_flow_burst_entry_ts: float = 0.0
    flow_burst_entry_minute: str | None = None
    flow_exit_minute: str | None = None
    minute_open_cvd: float | None = None
    current_minute_key: str | None = None
    prev_burst_poll_row: pd.Series | None = None
    pending_entry: PendingEntry | None = None


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


class TradeLogDeduper:
    """Skip duplicate round-trips on restart/replay."""

    def __init__(self, trades_path: Path) -> None:
        self._path = trades_path
        self._seen_full: set[tuple[str, str, str, str, str]] = set()
        self._seen_entry: set[tuple[str, str, str]] = set()
        if trades_path.exists():
            for line in trades_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._remember(json.loads(line))
                except json.JSONDecodeError:
                    continue

    @staticmethod
    def _normalize_price(value: Any) -> str:
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return str(value)

    def _remember(self, record: dict[str, Any]) -> None:
        entry_time = str(record.get("entry_time", ""))
        side = str(record.get("side", ""))
        entry_price = self._normalize_price(record.get("entry_price", ""))
        exit_time = str(record.get("exit_time", ""))
        exit_reason = str(record.get("exit_reason", ""))
        self._seen_full.add((entry_time, side, entry_price, exit_time, exit_reason))
        self._seen_entry.add((entry_time, side, entry_price))

    def is_duplicate(self, record: dict[str, Any]) -> bool:
        entry_time = str(record.get("entry_time", ""))
        side = str(record.get("side", ""))
        entry_price = self._normalize_price(record.get("entry_price", ""))
        exit_time = str(record.get("exit_time", ""))
        exit_reason = str(record.get("exit_reason", ""))
        full_key = (entry_time, side, entry_price, exit_time, exit_reason)
        entry_key = (entry_time, side, entry_price)
        return full_key in self._seen_full or entry_key in self._seen_entry

    def append(self, record: dict[str, Any]) -> bool:
        if self.is_duplicate(record):
            logger.debug(
                "Skipping duplicate trade log entry_time=%s side=%s",
                record.get("entry_time"),
                record.get("side"),
            )
            return False
        self._remember(record)
        _append_jsonl(self._path, record)
        return True


def _append_trade(
    trades_path: Path,
    trade: Trade,
    mode: str,
    deduper: TradeLogDeduper | None = None,
) -> None:
    record = _trade_record(trade, mode)
    if deduper is not None:
        deduper.append(record)
    else:
        _append_jsonl(trades_path, record)




def _nt8_position_matches_entry(nt8_pos: int | None, side: Side, quantity: int) -> bool:
    if nt8_pos is None or quantity <= 0:
        return False
    if side == Side.LONG:
        return nt8_pos >= quantity
    return nt8_pos <= -quantity


def _wait_gateway_entry_fill(
    gateway: LiveGateway,
    side: Side,
    quantity: int,
    *,
    order_type: str,
    attempts: int = 6,
    sleep_s: float = 0.35,
) -> bool:
    """Confirm NT8 holds the entry before the model keeps a position."""
    tries = attempts if str(order_type).upper() == "LIMIT" else max(3, attempts // 2)
    for attempt in range(tries):
        if _nt8_position_matches_entry(gateway.query_market_position(), side, quantity):
            return True
        if attempt + 1 < tries:
            time.sleep(sleep_s)
    return False

def _clear_position_if_gateway_flat(
    state: RunnerState,
    gateway: LiveGateway | None,
) -> None:
    """Drop in-memory position when NT8 confirms flat (post-block or restart)."""
    if gateway is None or state.position is None:
        return
    nt8_pos = gateway.query_market_position()
    if nt8_pos is None:
        return
    if nt8_pos == 0:
        logger.info(
            "Gateway flat — clearing phantom in-memory %s position",
            state.position.side.value,
        )
        state.position = None


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




def _fast_poll_sec(config: ScalperConfig) -> float:
    if config.is_mes_es_nq_mode():
        ms = min(
            config.raw_test_runtime.order_monitor_loop_ms,
            config.raw_test_runtime.exit_monitor_loop_ms,
        )
        return max(0.05, ms / 1000.0)
    return 2.0


def _decision_poll_sec(config: ScalperConfig, poll_seconds: float) -> float:
    if config.is_mes_es_nq_mode():
        return max(0.05, config.raw_test_runtime.decision_loop_ms / 1000.0)
    return poll_seconds


def _entry_cancel_timeout_sec(config: ScalperConfig) -> float:
    """MES raw test uses mes_execution.entry_timeout_ms; MNQ uses entry_cancel_timeout_sec."""
    if config.is_mes_es_nq_mode() and config.mes_execution.entry_timeout_ms > 0:
        return config.mes_execution.entry_timeout_ms / 1000.0
    return float(config.entry.entry_cancel_timeout_sec or 45)


def _fill_bucket_from_ms(elapsed_ms: float) -> str:
    if elapsed_ms < 400:
        return "fill_0_400ms"
    if elapsed_ms < 1000:
        return "fill_400_1000ms"
    if elapsed_ms < 1500:
        return "fill_1000_1500ms"
    return "fill_over_1500ms"


def _log_entry_event(log_dir: Path, record: dict[str, Any]) -> None:
    _append_jsonl(log_dir / "entry_events.jsonl", record)


def _read_mes_quote(config: ScalperConfig) -> dict[str, float | None]:
    from scalper.flow_signals import enrich_bar_from_orderflow

    snap = enrich_bar_from_orderflow(
        _execution_orderflow_root(config),
        pd.Series({"close": 0.0}),
        max_age_sec=config.mes_execution.quote_max_age_sec,
    )
    if snap is None:
        return {"bid": None, "ask": None, "mid": None, "spread_ticks": None, "quote_age_ms": None}
    bid = float(snap["bid"]) if pd.notna(snap.get("bid")) else None
    ask = float(snap["ask"]) if pd.notna(snap.get("ask")) else None
    mid = float(snap.get("close") or 0) or None
    if mid is None and bid and ask:
        mid = (bid + ask) / 2.0
    spread_ticks = None
    if bid is not None and ask is not None and config.tick_size > 0:
        spread_ticks = (ask - bid) / config.tick_size
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_ticks": spread_ticks,
        "quote_age_ms": None,
    }


def _signal_orderflow_root(config: ScalperConfig) -> str:
    return config.orderflow_root_for_signal()


def _execution_orderflow_root(config: ScalperConfig) -> str:
    return config.orderflow_root_for_execution()


def _execution_symbol(config: ScalperConfig) -> str:
    return config.execution_root()


def _resolve_mes_entry_price(signal, config: ScalperConfig) -> tuple[float | None, dict[str, Any]]:
    """Fresh MES bid/ask for marketable limit; returns (price, quote_meta)."""
    from scalper.flow_signals import enrich_bar_from_orderflow

    root = _execution_orderflow_root(config)
    snap = enrich_bar_from_orderflow(
        root,
        pd.Series({"close": signal.price}),
        max_age_sec=config.mes_execution.quote_max_age_sec,
    )
    meta: dict[str, Any] = {
        "mes_bid_at_submit": None,
        "mes_ask_at_submit": None,
        "mes_spread_at_submit": None,
        "cancel_reason": None,
    }
    if snap is None:
        meta["cancel_reason"] = "cancel_quote_stale"
        return None, meta
    bid = snap.get("bid")
    ask = snap.get("ask")
    if pd.notna(bid):
        meta["mes_bid_at_submit"] = float(bid)
    if pd.notna(ask):
        meta["mes_ask_at_submit"] = float(ask)
    if meta["mes_bid_at_submit"] is not None and meta["mes_ask_at_submit"] is not None:
        spread_ticks = (meta["mes_ask_at_submit"] - meta["mes_bid_at_submit"]) / config.tick_size
        meta["mes_spread_at_submit"] = spread_ticks
        if spread_ticks > config.mes_execution.max_spread_ticks:
            meta["cancel_reason"] = "cancel_spread"
            return None, meta
    chase = config.mes_execution.entry_chase_ticks * config.tick_size
    if signal.side == Side.LONG:
        if pd.notna(ask):
            return float(ask) + chase, meta
    else:
        if pd.notna(bid):
            return float(bid) - chase, meta
    px = float(snap.get("close") or signal.price or 0) or None
    if px is None or px <= 0:
        meta["cancel_reason"] = "cancel_quote_stale"
    return px, meta


def _resolve_entry_price(signal, config: ScalperConfig) -> tuple[float | None, dict[str, Any]]:
    """Logs and gateway price: live bid/ask when NT8 LIMIT demo entries are enabled."""
    if config.is_mes_es_nq_mode():
        mes_px, meta = _resolve_mes_entry_price(signal, config)
        if mes_px is None or mes_px <= 0:
            reason = meta.get("cancel_reason") or "cancel_quote_stale"
            logger.warning("MES entry quote unavailable (%s) — skipping entry", reason)
            return None, meta
        return mes_px, meta

    slippage_fill = _apply_slippage(
        signal.price, signal.side, True,
        config.backtest.slippage_ticks, config.tick_size,
    )
    try:
        import sys

        fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
        if fb_root not in sys.path:
            sys.path.insert(0, fb_root)
        from nt8_market_sanity import demo_limit_entries_enabled, limit_entry_price

        if demo_limit_entries_enabled():
            root = str(config.symbol or "MNQ").upper()[:3]
            live = limit_entry_price(root, signal.side.value, offset_ticks=0.0)
            if live and live > 0:
                return float(live), {}
            logger.warning(
                "limit entry price unavailable for %s %s (bar=%.2f) — skipping entry",
                signal.side.value,
                root,
                float(signal.price or 0),
            )
            return None, {}
    except Exception as exc:
        logger.debug("limit entry price fallback to slippage: %s", exc)
    return slippage_fill, {}


def _minute_key(ts: Any) -> str:
    if isinstance(ts, str):
        ts = pd.to_datetime(ts)
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)[:16]


def _intrabar_poll_sec(config: ScalperConfig) -> float:
    return max(
        float(config.entry.flow_burst_poll_sec or 0),
        float(config.entry.orderflow_poll_sec or 0),
    )


def _sync_minute_state(state: RunnerState, row: pd.Series, config: ScalperConfig) -> None:
    """Reset per-minute burst guards when the forming bar minute changes."""
    key = _minute_key(row["timestamp"])
    if state.current_minute_key == key:
        return
    state.current_minute_key = key
    state.flow_burst_entry_minute = None
    state.flow_exit_minute = None
    state.prev_burst_poll_row = None
    if config.entry.flow_burst_mode:
        snap, cvd_open = build_intrabar_snapshot_row(
            _signal_orderflow_root(config),
            minute_bar_row=row,
            cvd_at_minute_open=None,
        )
        state.minute_open_cvd = cvd_open if snap is not None else None


def _flow_burst_cooldown_active(state: RunnerState, config: ScalperConfig) -> bool:
    if not config.entry.use_burst_cooldown:
        return False
    if state.last_flow_burst_entry_ts <= 0:
        return False
    elapsed = time.monotonic() - state.last_flow_burst_entry_ts
    return elapsed < config.entry.flow_burst_cooldown_sec


def _min_seconds_between_entries_active(state: RunnerState, config: ScalperConfig) -> bool:
    min_sec = float(config.entry.min_seconds_between_entries or 0)
    if min_sec <= 0 or state.last_entry_submit_ts <= 0:
        return False
    return (time.monotonic() - state.last_entry_submit_ts) < min_sec


def _already_entered_this_minute(state: RunnerState, row: pd.Series) -> bool:
    key = _minute_key(row["timestamp"])
    return state.flow_burst_entry_minute == key


def _maybe_enrich_entry_row(
    row: pd.Series,
    config: ScalperConfig,
    state: RunnerState,
) -> pd.Series:
    """Poll orderflow.json on a timer and merge live depth/MBO into the entry row."""
    if not config.entry.use_flow_signals:
        return row
    now = time.monotonic()
    poll_sec = max(
        float(config.entry.orderflow_poll_sec or 0),
        float(config.entry.flow_burst_poll_sec or 0),
    )
    if poll_sec > 0 and now - state.last_orderflow_poll_ts < poll_sec:
        return row
    state.last_orderflow_poll_ts = now
    enriched = enrich_bar_from_orderflow(
        _signal_orderflow_root(config),
        row,
        max_age_sec=config.entry.orderflow_max_age_sec,
    )
    return enriched if enriched is not None else row


def _mark_flow_burst_entry(state: RunnerState, row: pd.Series) -> None:
    state.flow_burst_entry_minute = _minute_key(row["timestamp"])
    state.last_flow_burst_entry_ts = time.monotonic()


def _already_exited_this_minute(state: RunnerState, row: pd.Series) -> bool:
    return state.flow_exit_minute == _minute_key(row["timestamp"])


def _mark_flow_exit(state: RunnerState, row: pd.Series) -> None:
    state.flow_exit_minute = _minute_key(row["timestamp"])


def _execute_exit(
    row: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    risk: RiskManager,
    *,
    exit_price: float,
    reason: ExitReason,
    mode: str,
    trades_path: Path,
    gateway: LiveGateway | None,
    trade_deduper: TradeLogDeduper | None,
) -> Trade | None:
    if state.position is None:
        return None
    ts = row["timestamp"]
    if isinstance(ts, str):
        ts = pd.to_datetime(ts)
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
        bars_held=state.bar_index - state.position.entry_bar,
        entry_trend_score=state.entry_trend,
        entry_l2_score=state.entry_l2,
    )
    risk.record_trade(trade)
    _append_trade(trades_path, trade, mode, trade_deduper)
    if gateway is not None:
        gateway.submit_order(OrderRequest(
            action=OrderAction.EXIT,
            symbol=_execution_symbol(config),
            side=state.position.side,
            quantity=state.position.quantity,
            price=fill,
            reason=reason.value,
        ))
    cooldown_bars = config.entry.cooldown_bars_after_exit if config.filters.use_cooldown else 0
    state.position = None
    state.cooldown = cooldown_bars
    return trade


def _resolve_entry_signal(
    row: pd.Series,
    prev: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    i: int,
    ts: Any,
) -> Any:
    """Pick flow-burst or pullback entry path based on config."""
    if config.entry.use_per_minute_dedup and _already_entered_this_minute(state, row):
        return None
    prev_atr = float(prev.get("atr", 0))
    session_bar = state.session_bar
    bar_time = ts if isinstance(ts, datetime) else pd.to_datetime(ts)

    if config.entry.flow_burst_mode:
        return evaluate_flow_burst_entry(
            row, prev_atr, i, config, state.cooldown, session_bar, bar_time,
            prev_row=prev, trend_row=row,
        )
    if config.entry.pullback_mode:
        return evaluate_entry(
            row, prev_atr, i, config, state.cooldown, session_bar, bar_time,
            prev_row=prev,
        )
    return None


def _execute_entry(
    signal: Any,
    row: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    risk: RiskManager,
    *,
    mode: str,
    signals_path: Path,
    trades_path: Path,
    gateway: LiveGateway | None,
    trade_deduper: TradeLogDeduper | None,
    log_dir: Path | None = None,
) -> None:
    if config.is_mes_es_nq_mode():
        from scalper.mes_es_nq_runner import mes_entry_blocked_reason
        from scalper.nq_confirmation import nq_veto_comparison

        block = mes_entry_blocked_reason(
            gateway, config, pending_entry=state.pending_entry,
        )
        if block:
            logger.warning("MES entry blocked (%s)", block)
            return

    signal_ts = time.monotonic()
    mes_at_signal = _read_mes_quote(config) if config.is_mes_es_nq_mode() else {}
    fill, quote_meta = _resolve_entry_price(signal, config)
    if fill is None:
        if log_dir and config.is_mes_es_nq_mode():
            _log_entry_event(log_dir, {
                "event": "entry_skipped",
                "block_reason": quote_meta.get("cancel_reason") or "cancel_quote_stale",
                "es_signal_side": signal.side.value,
                "es_signal_price": signal.price,
                "mes_bid_at_signal": mes_at_signal.get("bid"),
                "mes_ask_at_signal": mes_at_signal.get("ask"),
                "mes_mid_at_signal": mes_at_signal.get("mid"),
                **quote_meta,
            })
        return
    qty = risk.position_size()
    ts = row["timestamp"]
    state.pending_entry = None
    state.position = init_position(signal.side, fill, state.bar_index, ts, qty, config)
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
        "nt8_demo_orders": demo_nt8_orders_enabled(),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    if config.is_mes_es_nq_mode():
        nq_cmp = nq_veto_comparison(signal.side, config)
        signal_record.update(
            {
                "strategy_mode": config.mode,
                "execution_instrument": config.execution_root(),
                "signal_instrument": config.signal_root(),
                "confirmation_instrument": config.confirmation_root(),
                "mes_entry_price": fill,
                "es_signal_side": signal.side.value,
                "es_signal_price": signal.price,
                "es_flow_reason": signal.reason,
                "mes_bid_at_signal": mes_at_signal.get("bid"),
                "mes_ask_at_signal": mes_at_signal.get("ask"),
                "mes_mid_at_signal": mes_at_signal.get("mid"),
                "mes_bid_at_submit": quote_meta.get("mes_bid_at_submit"),
                "mes_ask_at_submit": quote_meta.get("mes_ask_at_submit"),
                "mes_spread_at_submit": quote_meta.get("mes_spread_at_submit"),
                "entry_order_mode": config.mes_execution.entry_order_mode,
                "entry_allowed": True,
                **nq_cmp,
            }
        )
    _append_jsonl(signals_path, signal_record)
    logger.info("Signal: %s %s @ %s (%s)", signal.side.value, config.symbol, fill, signal.reason)

    if config.entry.use_one_entry_per_minute and "flow_burst" in signal.reason:
        _mark_flow_burst_entry(state, row)

    if gateway is not None:
        exec_sym = _execution_symbol(config)
        submit_ts = time.monotonic()
        result = gateway.submit_order(OrderRequest(
            action=OrderAction.ENTER,
            symbol=exec_sym,
            side=signal.side,
            quantity=qty,
            price=fill,
            reason=signal.reason,
        ))
        state.last_entry_submit_ts = submit_ts
        time_signal_to_submit_ms = (submit_ts - signal_ts) * 1000.0
        entry_event = {
            "event": "entry_submit",
            "order_id": result.get("order_id"),
            "status": result.get("status"),
            "entry_order_mode": result.get("entry_order_mode") or config.mes_execution.entry_order_mode,
            "entry_limit_price": result.get("limit_price") or fill,
            "time_signal_to_submit_ms": round(time_signal_to_submit_ms, 1),
            "es_signal_price": signal.price,
            "mes_bid_at_submit": result.get("mes_bid_at_submit") or quote_meta.get("mes_bid_at_submit"),
            "mes_ask_at_submit": result.get("mes_ask_at_submit") or quote_meta.get("mes_ask_at_submit"),
            "mes_spread_at_submit": result.get("mes_spread_at_submit") or quote_meta.get("mes_spread_at_submit"),
            "would_have_blocked_fill_divergence": result.get("would_have_blocked_fill_divergence"),
            "divergence_ticks": result.get("divergence_ticks"),
        }
        if log_dir and config.is_mes_es_nq_mode():
            _log_entry_event(log_dir, entry_event)
        if is_entry_blocked(result):
            logger.warning(
                "Entry blocked by gateway (%s) — rolling back in-memory position",
                result.get("status"),
            )
            state.position = None
            _clear_position_if_gateway_flat(state, gateway)
        elif _wait_gateway_entry_fill(
            gateway,
            signal.side,
            qty,
            order_type=str(result.get("order_type") or "MARKET"),
        ):
            fill_ts = time.monotonic()
            elapsed_ms = (fill_ts - submit_ts) * 1000.0
            if log_dir and config.is_mes_es_nq_mode():
                _log_entry_event(log_dir, {
                    **entry_event,
                    "event": "entry_filled",
                    "fill_bucket": _fill_bucket_from_ms(elapsed_ms),
                    "time_submit_to_fill_ms": round(elapsed_ms, 1),
                })
            if config.is_mes_es_nq_mode() and config.mes_execution.submit_hard_stop_on_fill and state.position:
                from scalper.exit_rules import stop_is_correct_side_of_entry, stop_side_metadata

                if not stop_is_correct_side_of_entry(
                    signal.side, state.position.entry_price, state.position.stop_price,
                ):
                    logger.critical(
                        "Stop on wrong side of entry — disabling entry side=%s entry=%.2f stop=%.2f",
                        signal.side.value,
                        state.position.entry_price,
                        state.position.stop_price,
                    )
                    state.position = None
                    return
                stop_meta = stop_side_metadata(
                    signal.side,
                    state.position.entry_price,
                    state.position.stop_price,
                    config.tick_size,
                )
                stop_start = time.monotonic()
                stop_result = gateway.submit_stop_order(
                    side=signal.side,
                    quantity=qty,
                    stop_price=state.position.stop_price,
                    reason="initial_hard_stop",
                    entry_price=state.position.entry_price,
                )
                stop_meta["stop_attach_latency_ms"] = round((time.monotonic() - stop_start) * 1000.0, 1)
                stop_meta["stop_order_id"] = stop_result.get("order_id")
                if log_dir:
                    _log_entry_event(log_dir, {"event": "stop_attach", **stop_meta})
        else:
            order_type = str(result.get("order_type") or "MARKET").upper()
            order_id = str(result.get("order_id") or "")
            if order_type == "LIMIT" and order_id:
                state.pending_entry = PendingEntry(
                    order_id=order_id,
                    submit_ts=submit_ts,
                    side=signal.side,
                    limit_price=fill,
                    quantity=qty,
                    reason=signal.reason,
                    signal_ts=signal_ts,
                    es_signal_price=float(signal.price),
                    mes_bid_at_signal=mes_at_signal.get("bid"),
                    mes_ask_at_signal=mes_at_signal.get("ask"),
                    mes_mid_at_signal=mes_at_signal.get("mid"),
                    mes_bid_at_submit=quote_meta.get("mes_bid_at_submit"),
                    mes_ask_at_submit=quote_meta.get("mes_ask_at_submit"),
                    entry_order_mode=str(
                        result.get("entry_order_mode") or config.mes_execution.entry_order_mode
                    ),
                )
                logger.info(
                    "LIMIT entry working (not filled yet) id=%s side=%s px=%.2f",
                    order_id, signal.side.value, fill,
                )
            logger.warning(
                "Entry submitted but NT8 not filled (%s) - rolling back model position",
                order_id or result.get("order_id"),
            )
            state.position = None
            _clear_position_if_gateway_flat(state, gateway)


def _maybe_poll_pending_entry_fill(
    row: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    gateway: LiveGateway | None,
    *,
    log_dir: Path | None = None,
) -> None:
    """Promote working LIMIT fill into in-memory position when NT8 confirms."""
    pending = state.pending_entry
    if pending is None or state.position is not None or gateway is None:
        return
    now = time.monotonic()
    if now - state.last_pending_fill_poll_ts < _fast_poll_sec(config):
        return
    state.last_pending_fill_poll_ts = now
    nt8_pos = gateway.query_market_position()
    if _nt8_position_matches_entry(nt8_pos, pending.side, pending.quantity):
        ts = row["timestamp"]
        if isinstance(ts, str):
            ts = pd.to_datetime(ts)
        state.position = init_position(
            pending.side, pending.limit_price, state.bar_index, ts, pending.quantity, config,
        )
        fill_ts = time.monotonic()
        elapsed_ms = (fill_ts - pending.submit_ts) * 1000.0
        if log_dir and config.is_mes_es_nq_mode():
            _log_entry_event(log_dir, {
                "event": "entry_filled",
                "order_id": pending.order_id,
                "fill_bucket": _fill_bucket_from_ms(elapsed_ms),
                "time_submit_to_fill_ms": round(elapsed_ms, 1),
                "entry_limit_price": pending.limit_price,
                "es_signal_price": pending.es_signal_price,
                "mes_mid_at_signal": pending.mes_mid_at_signal,
            })
        state.pending_entry = None
        logger.info("Pending LIMIT filled — position synced side=%s", state.position.side.value)


def _maybe_cancel_pending_entry_timeout(
    state: RunnerState,
    config: ScalperConfig,
    gateway: LiveGateway | None,
    *,
    log_dir: Path | None = None,
) -> None:
    """Fast timeout cancel — runs before any slow NT8/file work in the follow loop."""
    pending = state.pending_entry
    if pending is None or state.position is not None or gateway is None:
        return
    elapsed = time.monotonic() - pending.submit_ts
    timeout = _entry_cancel_timeout_sec(config)
    if elapsed < timeout:
        return
    gateway.cancel_order(pending.order_id, reason="cancel_timeout")
    if log_dir and config.is_mes_es_nq_mode():
        _log_entry_event(log_dir, {
            "event": "entry_cancelled",
            "cancel_reason": "cancel_timeout",
            "fill_bucket": "cancel_timeout",
            "order_id": pending.order_id,
            "time_submit_to_cancel_ms": round(elapsed * 1000.0, 1),
            "entry_limit_price": pending.limit_price,
        })
    state.pending_entry = None
    logger.info("Cancelled pending LIMIT (timeout %.0fms) id=%s", timeout * 1000, pending.order_id)


def _maybe_poll_adverse_entry_cancel(
    row: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    gateway: LiveGateway | None,
    *,
    log_dir: Path | None = None,
) -> None:
    """Cancel working entry LIMIT on adverse MES mid move (timeout handled separately)."""
    pending = state.pending_entry
    if pending is None or state.position is not None or gateway is None:
        return

    if not config.entry.use_adverse_mid_cancel:
        return

    now = time.monotonic()
    if now - state.last_fast_monitor_ts < _fast_poll_sec(config):
        return
    state.last_fast_monitor_ts = now
    elapsed = now - pending.submit_ts

    mes_quote = _read_mes_quote(config) if config.is_mes_es_nq_mode() else {}
    mid = float(mes_quote.get("mid") or 0)
    if mid <= 0 and config.entry.use_flow_signals:
        snap, _ = build_intrabar_snapshot_row(
            _execution_orderflow_root(config) if config.is_mes_es_nq_mode() else _signal_orderflow_root(config),
            minute_bar_row=row,
            cvd_at_minute_open=state.minute_open_cvd,
            prev_poll_row=state.prev_burst_poll_row,
        )
        if snap is not None:
            mid = float(snap.get("close") or 0)

    adverse_ticks = int(config.entry.entry_adverse_mid_ticks or 9)
    if adverse_entry_mid_moved_against(
        mid, pending.limit_price, pending.side, config.tick_size, adverse_ticks,
    ):
        gateway.cancel_order(pending.order_id, reason="cancel_adverse_mid")
        if log_dir and config.is_mes_es_nq_mode():
            _log_entry_event(log_dir, {
                "event": "entry_cancelled",
                "cancel_reason": "cancel_adverse_mid",
                "side": pending.side.value,
                "entry_limit_price": pending.limit_price,
                "mes_mid_at_submit": pending.mes_mid_at_signal,
                "mes_mid_at_cancel": mid,
                "adverse_ticks": adverse_ticks,
                "time_working_ms": round(elapsed * 1000.0, 1),
                "order_id": pending.order_id,
            })
        state.pending_entry = None
        logger.info(
            "Cancelled pending LIMIT (adverse mid %dt) id=%s side=%s",
            adverse_ticks, pending.order_id, pending.side.value,
        )


def _maybe_poll_flow_exit(
    row: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    risk: RiskManager,
    *,
    mode: str,
    trades_path: Path,
    gateway: LiveGateway | None,
    trade_deduper: TradeLogDeduper | None,
) -> None:
    """Poll orderflow.json between bar closes for flow-based intrabar exits."""
    if state.position is None or not config.entry.use_flow_signals:
        return
    if _already_exited_this_minute(state, row):
        return

    if gateway is not None:
        _clear_position_if_gateway_flat(state, gateway)
        if state.position is None:
            return

    now = time.monotonic()
    poll_sec = _intrabar_poll_sec(config)
    if now - state.last_flow_exit_poll_ts < _fast_poll_sec(config):
        return
    state.last_flow_exit_poll_ts = now

    snap, cvd_open = build_intrabar_snapshot_row(
        _execution_orderflow_root(config) if config.is_mes_es_nq_mode() else _signal_orderflow_root(config),
        minute_bar_row=row,
        cvd_at_minute_open=state.minute_open_cvd,
        prev_poll_row=state.prev_burst_poll_row,
    )
    if snap is None:
        return
    if state.minute_open_cvd is None:
        state.minute_open_cvd = cvd_open

    exit_price, reason = evaluate_flow_exit(state.position, snap, config)
    if exit_price is None or reason is None:
        return

    _mark_flow_exit(state, row)
    _execute_exit(
        row, state, config, risk,
        exit_price=exit_price,
        reason=reason,
        mode=mode,
        trades_path=trades_path,
        gateway=gateway,
        trade_deduper=trade_deduper,
    )
    logger.info("Flow intrabar exit: %s @ %.2f", reason.value, exit_price)


def _maybe_poll_flow_burst(
    row: pd.Series,
    prev: pd.Series,
    state: RunnerState,
    config: ScalperConfig,
    risk: RiskManager,
    *,
    mode: str,
    signals_path: Path,
    trades_path: Path,
    gateway: LiveGateway | None,
    trade_deduper: TradeLogDeduper | None,
    log_dir: Path | None = None,
) -> None:
    """Poll ES orderflow between 1m bar closes for momentum burst entries."""
    if not config.entry.flow_burst_mode:
        return
    if state.position is not None or state.cooldown > 0 or not risk.can_enter():
        return
    if state.pending_entry is not None:
        return
    if config.is_mes_es_nq_mode():
        from scalper.mes_es_nq_runner import mes_entry_blocked_reason

        block = mes_entry_blocked_reason(
            gateway, config, pending_entry=state.pending_entry,
        )
        if block:
            return
    if _flow_burst_cooldown_active(state, config):
        return
    if _min_seconds_between_entries_active(state, config):
        return
    if config.entry.use_one_entry_per_minute and _already_entered_this_minute(state, row):
        return

    now = time.monotonic()
    poll_sec = _intrabar_poll_sec(config)
    if poll_sec > 0 and now - state.last_flow_burst_poll_ts < poll_sec:
        return
    state.last_flow_burst_poll_ts = now

    snap, cvd_open = build_intrabar_snapshot_row(
        _signal_orderflow_root(config),
        minute_bar_row=row,
        cvd_at_minute_open=state.minute_open_cvd,
        prev_poll_row=state.prev_burst_poll_row,
    )
    if snap is None:
        return
    if state.minute_open_cvd is None:
        state.minute_open_cvd = cvd_open

    prev_snap = state.prev_burst_poll_row if state.prev_burst_poll_row is not None else prev
    ts = row["timestamp"]
    bar_time = ts if isinstance(ts, datetime) else pd.to_datetime(ts)
    signal = evaluate_flow_burst_entry(
        snap,
        float(prev.get("atr", 0) or 0),
        state.bar_index,
        config,
        state.cooldown,
        state.session_bar,
        bar_time,
        prev_row=prev_snap,
        trend_row=row,
    )
    state.prev_burst_poll_row = snap.copy()
    if signal is None:
        return

    _execute_entry(
        signal, snap, state, config, risk,
        mode=mode,
        signals_path=signals_path,
        trades_path=trades_path,
        gateway=gateway,
        trade_deduper=trade_deduper,
        log_dir=log_dir,
    )


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
    trade_deduper: TradeLogDeduper | None = None,
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
        _clear_position_if_gateway_flat(state, gateway)
        if state.position is None:
            state.bar_index += 1
            return new_trades
        exit_price, reason = evaluate_exit(state.position, row, i, ts, config)
        if exit_price is not None and reason is not None:
            _mark_flow_exit(state, row)
            trade = _execute_exit(
                row, state, config, risk,
                exit_price=exit_price,
                reason=reason,
                mode=mode,
                trades_path=trades_path,
                gateway=gateway,
                trade_deduper=trade_deduper,
            )
            if trade is not None:
                new_trades.append(trade)
            state.bar_index += 1
            return new_trades
        state.bar_index += 1
        return new_trades

    if not risk.can_enter():
        state.bar_index += 1
        return new_trades

    if state.pending_entry is not None:
        state.bar_index += 1
        return new_trades

    _sync_minute_state(state, row, config)
    entry_row = _maybe_enrich_entry_row(row, config, state)
    signal = _resolve_entry_signal(entry_row, prev, state, config, i, ts)
    if signal is None:
        state.bar_index += 1
        return new_trades

    _execute_entry(
        signal, row, state, config, risk,
        mode=mode,
        signals_path=signals_path,
        trades_path=trades_path,
        gateway=gateway,
        trade_deduper=trade_deduper,
    )

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
    trade_deduper = TradeLogDeduper(trades_path)
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
            trade_deduper=trade_deduper,
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
        _append_trade(trades_path, trade, "replay", trade_deduper)

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


def _checkpoint_path() -> Path:
    state_dir = Path(os.getenv("L2_SCALPER_STATE_DIR", r"C:\Bots\futures-trend-day-l2-scalper\state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "paper_runner_checkpoint.json"


def _load_follow_checkpoint() -> dict[str, Any]:
    path = _checkpoint_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_follow_checkpoint(timestamp: str, state: RunnerState) -> None:
    path = _checkpoint_path()
    payload: dict[str, Any] = {
        "last_processed_timestamp": timestamp,
        "session_bar": state.session_bar,
    }
    if state.prev_session_date is not None:
        payload["prev_session_date"] = str(state.prev_session_date)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _init_follow_session_state(df: pd.DataFrame, state: RunnerState) -> int:
    """Seed session_bar from CSV so restart does not re-block min_bars_after_open."""
    if state.processed_timestamps is None:
        state.processed_timestamps = set()
    timestamps = df["timestamp"].astype(str)
    if len(timestamps) == 0:
        return 0

    last_ts = pd.to_datetime(timestamps.iloc[-1])
    session_date = last_ts.date()
    state.prev_session_date = session_date
    bars_processed = sum(
        1
        for ts in timestamps
        if ts in state.processed_timestamps and pd.to_datetime(ts).date() == session_date
    )
    state.session_bar = bars_processed
    return bars_processed


def _seed_follow_processed_bars(df: pd.DataFrame, state: RunnerState) -> str:
    """Mark all bars before the last closed bar as processed; leave last bar eligible."""
    if state.processed_timestamps is None:
        state.processed_timestamps = set()
    timestamps = df["timestamp"].astype(str)
    for ts in timestamps.iloc[:-1]:
        state.processed_timestamps.add(ts)
    return str(timestamps.iloc[-1])


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
    trade_deduper = TradeLogDeduper(trades_path)
    risk = RiskManager(config)
    state = RunnerState(processed_timestamps=set())
    history = pd.DataFrame()
    initialized = False
    loop_sec = _decision_poll_sec(config, poll_seconds)

    logger.info(
        "Following %s (poll=%ss, fast_monitor=%ss, paper_only=%s)",
        data_path,
        loop_sec,
        _fast_poll_sec(config),
        paper_only_mode(),
    )

    while True:
        _maybe_cancel_pending_entry_timeout(state, config, gateway, log_dir=log_dir)

        if not data_path.exists():
            logger.warning(
                "Bar file missing: %s â€” enable ScalperL2Exporter in NT8 (see integrations/ninjatrader8/README.md)",
                data_path,
            )
            time.sleep(loop_sec)
            continue

        try:
            df = load_bars(data_path)
        except Exception as exc:
            logger.warning("Could not read bars: %s", exc)
            time.sleep(loop_sec)
            continue

        if len(df) < 2:
            time.sleep(loop_sec)
            continue

        df = compute_indicators(df, config.trend)
        if not initialized:
            session_date = pd.to_datetime(df["timestamp"].iloc[-1]).date()
            risk.hydrate_from_jsonl(trades_path, session_date=session_date, mode="follow")
            last_eligible = _seed_follow_processed_bars(df, state)
            session_bars_processed = _init_follow_session_state(df, state)
            checkpoint = _load_follow_checkpoint()
            history = df.copy()
            initialized = True
            logger.info(
                "Follow startup: marked %d prior bars processed; last bar %s eligible; "
                "session_bar=%d (today processed=%d, checkpoint=%s); "
                "risk trades_today=%d halted=%s reason=%s",
                len(df) - 1,
                last_eligible,
                state.session_bar,
                session_bars_processed,
                checkpoint.get("session_bar", "-"),
                risk.trades_today,
                risk.halted,
                risk.halt_reason or "-",
            )
            _clear_position_if_gateway_flat(state, gateway)
            if gateway is not None:
                gateway.cancel_orphan_orders(reason="follow_startup_reconcile")

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
                trade_deduper=trade_deduper,
            )
            state.processed_timestamps.add(ts_key)
            _save_follow_checkpoint(ts_key, state)

        if initialized and len(df) >= 2:
            last_row = df.iloc[-1]
            prev_row = df.iloc[-2]
            state.bar_index = len(df) - 1
            _sync_minute_state(state, last_row, config)
            _maybe_poll_adverse_entry_cancel(last_row, state, config, gateway, log_dir=log_dir)
            _maybe_poll_pending_entry_fill(last_row, state, config, gateway, log_dir=log_dir)
            _maybe_poll_flow_exit(
                last_row, state, config, risk,
                mode="follow",
                trades_path=trades_path,
                gateway=gateway,
                trade_deduper=trade_deduper,
            )
            _maybe_poll_flow_burst(
                last_row, prev_row, state, config, risk,
                mode="follow",
                signals_path=signals_path,
                trades_path=trades_path,
                gateway=gateway,
                trade_deduper=trade_deduper,
                log_dir=log_dir,
            )

        if state.pending_entry is not None:
            timeout_sec = _entry_cancel_timeout_sec(config)
            monitor_deadline = state.pending_entry.submit_ts + timeout_sec + 0.15
            monitor_row = df.iloc[-1] if initialized and len(df) >= 1 else None
            while state.pending_entry is not None and time.monotonic() < monitor_deadline:
                _maybe_cancel_pending_entry_timeout(state, config, gateway, log_dir=log_dir)
                if monitor_row is not None:
                    _maybe_poll_pending_entry_fill(
                        monitor_row, state, config, gateway, log_dir=log_dir,
                    )
                if state.pending_entry is None:
                    break
                time.sleep(_fast_poll_sec(config))
        else:
            time.sleep(loop_sec)


def _enforce_runner_safety() -> None:
    """Block duplicate system-python roots; allow Windows venv base-python children."""
    if os.name != "nt":
        return
    exe = Path(sys.executable).resolve()
    if ".venv" in str(exe).replace("\\", "/").lower():
        return
    venv_root = os.getenv("VIRTUAL_ENV", "").strip()
    if venv_root and Path(venv_root).is_dir():
        return
    raise SystemExit(
        f"Refusing paper_runner outside install venv (python={exe}); "
        "start via scripts/start_l2_scalper.ps1"
    )


def _acquire_single_instance() -> None:
    """One paper_runner per machine; pidfile under install state/."""
    if os.name != "nt":
        return
    import atexit
    import ctypes

    mutex_name = r"Global\FuturesBot.L2Scalper.PaperRunner"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    if kernel32.GetLastError() == 183:
        raise SystemExit("paper_runner already running (mutex)")

    state_dir = Path(os.getenv("L2_SCALPER_STATE_DIR", r"C:\\Bots\\futures-trend-day-l2-scalper\\state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / "paper_runner.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")

    def _cleanup() -> None:
        try:
            if pid_file.exists() and pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        except Exception:
            pass

    atexit.register(_cleanup)


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

    _enforce_runner_safety()
    _acquire_single_instance()

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    gateway = LiveGateway(log_dir=log_dir)
    gateway.connect()
    gateway.cancel_orphan_orders(reason="paper_runner_startup")

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
