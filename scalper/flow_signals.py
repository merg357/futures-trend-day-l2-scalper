"""Order-flow entry signals from CSV bar fields (delta, book size, depth).

Live runner also builds intrabar snapshots from state/orderflow.json DOM fields
(Flow / L2-DOM proxy — NOT live MBO parquet from data/mbo/MNQ/).

Research (l2_mbo_circled_moves_research_latest.md): circled moves were momentum
bursts (trade_delta, depth churn 8k-13k events/min, MBO bid/ask add skew) — not
consistent EMA pullbacks. MBO parquet patterns are proxied here on L0 book + CVD.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from scalper.config import FlowConfig
from scalper.models import Side

# Live DOM from futures_exec — not Rithmic MBO parquet archive.
ORDERFLOW_PATH = Path(os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")) / "state" / "orderflow.json"
SIGNALS_PATH = Path(os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")) / "state" / "signals.json"
ORDERFLOW_MAX_AGE_SEC = 45.0


@dataclass
class FlowSignal:
    side: Side | None
    score: float
    triggers_hit: int
    triggers: dict[str, bool] = field(default_factory=dict)
    components: dict[str, float] = field(default_factory=dict)


def _num(row: pd.Series, key: str, default: float = 0.0) -> float:
    val = row.get(key, default)
    if pd.isna(val):
        return default
    return float(val)


def _total_l0_depth(row: pd.Series) -> float:
    return _num(row, "bid_size") + _num(row, "ask_size")


def _total_book_depth(row: pd.Series) -> float:
    bid_d = _num(row, "bid_depth")
    ask_d = _num(row, "ask_depth")
    if bid_d + ask_d > 0:
        return bid_d + ask_d
    return _total_l0_depth(row)


def _short_delta_hit(row: pd.Series, cfg: FlowConfig) -> bool:
    return _num(row, "delta") <= cfg.short_delta_max


def _long_delta_hit(row: pd.Series, cfg: FlowConfig) -> bool:
    return _num(row, "delta") >= cfg.long_delta_min


def _short_imbalance_hit(row: pd.Series, cfg: FlowConfig) -> bool:
    imb = row.get("imbalance")
    if pd.notna(imb) and float(imb) <= -cfg.imbalance_threshold:
        return True
    mbo_bid = row.get("mbo_bid_new_count")
    mbo_ask = row.get("mbo_ask_new_count")
    if pd.notna(mbo_bid) and pd.notna(mbo_ask):
        bid_new = float(mbo_bid)
        ask_new = float(mbo_ask)
        if bid_new > 0 and ask_new / bid_new >= cfg.book_size_ratio:
            return True
    bid_sz = _num(row, "bid_size")
    ask_sz = _num(row, "ask_size")
    if bid_sz > 0 and ask_sz / bid_sz >= cfg.book_size_ratio:
        return True
    return False


def _long_imbalance_hit(row: pd.Series, cfg: FlowConfig) -> bool:
    imb = row.get("imbalance")
    if pd.notna(imb) and float(imb) >= cfg.imbalance_threshold:
        return True
    mbo_bid = row.get("mbo_bid_new_count")
    mbo_ask = row.get("mbo_ask_new_count")
    if pd.notna(mbo_bid) and pd.notna(mbo_ask):
        bid_new = float(mbo_bid)
        ask_new = float(mbo_ask)
        if ask_new > 0 and bid_new / ask_new >= cfg.book_size_ratio:
            return True
    bid_sz = _num(row, "bid_size")
    ask_sz = _num(row, "ask_size")
    if ask_sz > 0 and bid_sz / ask_sz >= cfg.book_size_ratio:
        return True
    return False


def _short_ask_shrink_hit(row: pd.Series, prev_bar: pd.Series | None, cfg: FlowConfig) -> bool:
    if prev_bar is None:
        return False
    ask_sz = _num(row, "ask_size")
    prev_ask = _num(prev_bar, "ask_size")
    if prev_ask > 0 and ask_sz <= prev_ask * (1.0 - cfg.ask_shrink_min_drop):
        return True
    delta = _num(row, "delta")
    if delta < 0 and ask_sz > prev_ask:
        return True
    return False


def _long_depth_burst_hit(row: pd.Series, prev_bar: pd.Series | None, cfg: FlowConfig) -> bool:
    delta = _num(row, "delta")
    if delta >= cfg.strong_delta_magnitude:
        return True
    if prev_bar is None:
        return False
    total = _total_book_depth(row)
    prev_total = _total_book_depth(prev_bar)
    if prev_total > 0 and total >= prev_total * cfg.depth_spike_ratio:
        return True
    return False


def _score_side(triggers: dict[str, bool], weights: dict[str, float]) -> float:
    raw = sum(weights[k] for k, hit in triggers.items() if hit)
    return min(100.0, raw)


def _evaluate_short(row: pd.Series, prev_bar: pd.Series | None, cfg: FlowConfig) -> FlowSignal:
    triggers = {
        "delta": _short_delta_hit(row, cfg),
        "imbalance": _short_imbalance_hit(row, cfg),
        "ask_shrink": _short_ask_shrink_hit(row, prev_bar, cfg),
    }
    weights = {
        "delta": cfg.weight_delta,
        "imbalance": cfg.weight_imbalance,
        "ask_shrink": cfg.weight_book_change,
    }
    hits = sum(1 for v in triggers.values() if v)
    score = _score_side(triggers, weights)
    return FlowSignal(
        side=Side.SHORT if hits >= cfg.min_triggers or score >= cfg.min_flow_score else None,
        score=score,
        triggers_hit=hits,
        triggers=triggers,
        components={k: weights[k] if v else 0.0 for k, v in triggers.items()},
    )


def _evaluate_long(row: pd.Series, prev_bar: pd.Series | None, cfg: FlowConfig) -> FlowSignal:
    triggers = {
        "delta": _long_delta_hit(row, cfg),
        "depth_burst": _long_depth_burst_hit(row, prev_bar, cfg),
        "imbalance": _long_imbalance_hit(row, cfg),
    }
    weights = {
        "delta": cfg.weight_delta,
        "depth_burst": cfg.weight_book_change,
        "imbalance": cfg.weight_imbalance,
    }
    hits = sum(1 for v in triggers.values() if v)
    score = _score_side(triggers, weights)
    return FlowSignal(
        side=Side.LONG if hits >= cfg.min_triggers or score >= cfg.min_flow_score else None,
        score=score,
        triggers_hit=hits,
        triggers=triggers,
        components={k: weights[k] if v else 0.0 for k, v in triggers.items()},
    )


def compute_flow_for_side(
    bar: pd.Series,
    prev_bar: pd.Series | None,
    side: Side,
    cfg: FlowConfig,
) -> FlowSignal:
    """Evaluate circled-move triggers for one side only."""
    if side == Side.LONG:
        return _evaluate_long(bar, prev_bar, cfg)
    return _evaluate_short(bar, prev_bar, cfg)


def compute_flow_signal(
    bar: pd.Series,
    prev_bar: pd.Series | None,
    cfg: FlowConfig,
) -> FlowSignal:
    """Return the stronger directional flow signal (both sides scored)."""
    long_sig = _evaluate_long(bar, prev_bar, cfg)
    short_sig = _evaluate_short(bar, prev_bar, cfg)

    long_pass = long_sig.side == Side.LONG
    short_pass = short_sig.side == Side.SHORT

    if long_pass and (not short_pass or long_sig.score >= short_sig.score):
        return long_sig
    if short_pass:
        return short_sig
    if long_sig.score >= short_sig.score:
        return FlowSignal(
            side=None,
            score=long_sig.score,
            triggers_hit=long_sig.triggers_hit,
            triggers=long_sig.triggers,
            components=long_sig.components,
        )
    return FlowSignal(
        side=None,
        score=short_sig.score,
        triggers_hit=short_sig.triggers_hit,
        triggers=short_sig.triggers,
        components=short_sig.components,
    )


def flow_supports_side(flow: FlowSignal, side: Side, cfg: FlowConfig) -> bool:
    if flow.side is not None and flow.side != side:
        return False
    return flow.triggers_hit >= cfg.min_triggers or flow.score >= cfg.min_flow_score


def flow_burst_passes(flow: FlowSignal, side: Side, cfg: FlowConfig) -> bool:
    """Burst entry requires both score and trigger count (research: 2-of-3 + min score)."""
    if flow.side != side:
        return False
    return flow.score >= cfg.min_flow_score and flow.triggers_hit >= cfg.min_triggers


def _read_orderflow_instrument(
    root: str,
    *,
    path: Path | None = None,
    max_age_sec: float | None = None,
) -> dict | None:
    """Read one instrument row from futures_exec orderflow.json (L2 DOM, not MBO parquet)."""
    of_path = path or ORDERFLOW_PATH
    if not of_path.exists():
        return None
    try:
        import time

        age_limit = ORDERFLOW_MAX_AGE_SEC if max_age_sec is None else max_age_sec
        age = time.time() - of_path.stat().st_mtime
        if age > age_limit:
            return None
        data = json.loads(of_path.read_text(encoding="utf-8"))
        row = data.get(root.upper()) or data.get(root)
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def _merge_mbo_from_signals(root: str, mnq: dict) -> dict:
    """Overlay signals.json mbo_features when fresher or orderflow row lacks counters."""
    if not SIGNALS_PATH.exists():
        return mnq
    try:
        payload = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        sig_row = payload.get(root.upper()) or payload.get(root) or {}
        sig_mbo = sig_row.get("mbo_features") if isinstance(sig_row, dict) else None
        if not isinstance(sig_mbo, dict) or not sig_mbo:
            return mnq
        merged = dict(mnq)
        existing = merged.get("mbo_features")
        if not isinstance(existing, dict) or int(existing.get("mbo_event_count") or 0) <= 0:
            merged["mbo_features"] = sig_mbo
        for key in (
            "mbo_bid_new_count",
            "mbo_ask_new_count",
            "mbo_event_count",
            "mbo_new_imbalance",
            "mbo_bid_cancel_rate_per_min",
            "mbo_ask_cancel_rate_per_min",
            "mbo_cancel_rate_per_min",
        ):
            if merged.get(key) in (None, 0) and sig_mbo.get(key) is not None:
                merged[key] = sig_mbo.get(key)
        return merged
    except Exception:
        return mnq


def _scalar_orderflow_fields(mnq: dict) -> dict[str, float]:
    """Flatten live orderflow/MBO scalars for pandas bar rows."""
    mbo = mnq.get("mbo_features") if isinstance(mnq.get("mbo_features"), dict) else mnq
    bid_levels = mnq.get("bid_levels") or []
    ask_levels = mnq.get("ask_levels") or []
    bid_size = float(sum(int(x.get("qty") or 0) for x in bid_levels))
    ask_size = float(sum(int(x.get("qty") or 0) for x in ask_levels))
    total_sz = bid_size + ask_size
    imbalance = (bid_size - ask_size) / total_sz if total_sz > 0 else 0.0
    return {
        "obi": float(mnq.get("obi") or 0.0),
        "tape_ratio": float(mnq.get("tape_ratio") or 0.0),
        "bid_size": bid_size,
        "ask_size": ask_size,
        "bid_depth": float(mnq.get("total_bid_vol") or bid_size),
        "ask_depth": float(mnq.get("total_ask_vol") or ask_size),
        "imbalance": imbalance,
        "mbo_bid_new_count": float(mbo.get("mbo_bid_new_count") or mnq.get("mbo_bid_new_count") or 0),
        "mbo_ask_new_count": float(mbo.get("mbo_ask_new_count") or mnq.get("mbo_ask_new_count") or 0),
        "mbo_event_count": float(mbo.get("mbo_event_count") or mnq.get("mbo_event_count") or 0),
        "mbo_new_imbalance": float(mbo.get("mbo_new_imbalance") or mnq.get("mbo_new_imbalance") or 0)
        if (mbo.get("mbo_new_imbalance") is not None or mnq.get("mbo_new_imbalance") is not None)
        else float("nan"),
        "mbo_bid_cancel_rate_per_min": float(
            mbo.get("mbo_bid_cancel_rate_per_min") or mnq.get("mbo_bid_cancel_rate_per_min") or 0
        ),
        "mbo_ask_cancel_rate_per_min": float(
            mbo.get("mbo_ask_cancel_rate_per_min") or mnq.get("mbo_ask_cancel_rate_per_min") or 0
        ),
        "mbo_cancel_rate_per_min": float(
            mbo.get("mbo_cancel_rate_per_min") or mnq.get("mbo_cancel_rate_per_min") or 0
        ),
        "depth_event_count_60s": float(mnq.get("depth_event_count_60s") or 0),
        "depth_event_rate_per_min": float(mnq.get("depth_event_rate_per_min") or 0),
        "aggressive_buy_volume_60s": float(mnq.get("aggressive_buy_volume_60s") or 0),
        "aggressive_sell_volume_60s": float(mnq.get("aggressive_sell_volume_60s") or 0),
        "net_aggressive_volume_60s": float(mnq.get("net_aggressive_volume_60s") or 0),
    }


def enrich_bar_from_orderflow(
    symbol_root: str,
    bar_row: pd.Series,
    *,
    orderflow_path: Path | None = None,
    max_age_sec: float | None = None,
) -> pd.Series | None:
    """Merge live multi-level orderflow + MBO counters into a bar row for entry gates."""
    mnq = _read_orderflow_instrument(
        symbol_root,
        path=orderflow_path,
        max_age_sec=max_age_sec,
    )
    if mnq is None:
        return None
    mnq = _merge_mbo_from_signals(symbol_root, mnq)
    snap = bar_row.copy()
    fields = _scalar_orderflow_fields(mnq)
    for key, value in fields.items():
        snap[key] = value
    bid_levels = mnq.get("bid_levels") or []
    ask_levels = mnq.get("ask_levels") or []
    if bid_levels:
        snap["bid"] = float(bid_levels[0]["price"])
    if ask_levels:
        snap["ask"] = float(ask_levels[0]["price"])
    cvd_now = float(mnq.get("cvd") or 0)
    if cvd_now and (pd.isna(snap.get("delta")) or float(snap.get("delta") or 0) == 0.0):
        snap["delta"] = float(mnq.get("cvd_recent") or 0)
    return snap


def build_intrabar_snapshot_row(
    symbol_root: str,
    *,
    minute_bar_row: pd.Series,
    cvd_at_minute_open: float | None,
    prev_poll_row: pd.Series | None = None,
    orderflow_path: Path | None = None,
) -> tuple[pd.Series | None, float]:
    """Build a synthetic bar row from live orderflow.json for intrabar flow burst.

    Uses DOM bid/ask levels + CVD + MBO counters from futures_exec (L2/DOM proxy).
    """
    mnq = _read_orderflow_instrument(symbol_root, path=orderflow_path)
    if mnq is None:
        return None, cvd_at_minute_open or 0.0
    mnq = _merge_mbo_from_signals(symbol_root, mnq)

    bid_levels = mnq.get("bid_levels") or []
    ask_levels = mnq.get("ask_levels") or []
    mid = float(mnq.get("mid_price") or 0)
    if mid <= 0 and bid_levels and ask_levels:
        mid = (float(bid_levels[0]["price"]) + float(ask_levels[0]["price"])) / 2.0
    elif mid <= 0 and bid_levels:
        mid = float(bid_levels[0]["price"])
    elif mid <= 0 and ask_levels:
        mid = float(ask_levels[0]["price"])
    if mid <= 0:
        mid = float(minute_bar_row.get("close") or 0)
    if mid <= 0:
        return None, cvd_at_minute_open or 0.0

    tick_spread = float(mnq.get("spread") or 0.25) or 0.25
    bid = float(bid_levels[0]["price"]) if bid_levels else mid - tick_spread / 2
    ask = float(ask_levels[0]["price"]) if ask_levels else mid + tick_spread / 2

    bid_size = float(sum(int(x.get("qty") or 0) for x in bid_levels))
    ask_size = float(sum(int(x.get("qty") or 0) for x in ask_levels))
    bid_depth = float(mnq.get("total_bid_vol") or bid_size)
    ask_depth = float(mnq.get("total_ask_vol") or ask_size)

    cvd_now = float(mnq.get("cvd") or 0)
    cvd_open = cvd_at_minute_open if cvd_at_minute_open is not None else cvd_now
    delta = cvd_now - cvd_open
    if delta == 0.0:
        delta = float(mnq.get("cvd_recent") or 0)

    total_sz = bid_size + ask_size
    imbalance = (bid_size - ask_size) / total_sz if total_sz > 0 else 0.0

    snap = minute_bar_row.copy()
    snap["open"] = mid
    snap["high"] = max(float(snap.get("high", mid)), mid, ask)
    snap["low"] = min(float(snap.get("low", mid)), mid, bid)
    snap["close"] = mid
    snap["bid"] = bid
    snap["ask"] = ask
    snap["bid_size"] = bid_size
    snap["ask_size"] = ask_size
    snap["bid_depth"] = bid_depth
    snap["ask_depth"] = ask_depth
    snap["delta"] = delta
    snap["imbalance"] = imbalance
    for key, value in _scalar_orderflow_fields(mnq).items():
        if key not in {"bid_size", "ask_size", "bid_depth", "ask_depth", "imbalance"}:
            snap[key] = value
    return snap, cvd_open
