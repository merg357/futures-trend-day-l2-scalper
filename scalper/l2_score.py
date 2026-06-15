"""L2 order-book score (0-100) with approximation fallback."""

from __future__ import annotations

import pandas as pd

from scalper.config import L2Config
from scalper.models import Bias, L2Score, Side


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _has_l2_columns(row: pd.Series) -> bool:
    cols = ("bid_size", "ask_size", "bid_depth", "ask_depth")
    return all(c in row.index and pd.notna(row.get(c)) for c in cols)


def score_imbalance(row: pd.Series, side: Side, cfg: L2Config) -> float:
    imb = row.get("imbalance")
    if pd.isna(imb):
        return 50.0
    if side == Side.LONG:
        if imb >= cfg.imbalance_threshold:
            return _clamp(50 + imb * 50)
        return _clamp(imb * 50)
    if imb <= -cfg.imbalance_threshold:
        return _clamp(50 + abs(imb) * 50)
    return _clamp((1 - abs(imb)) * 40)


def score_depth(row: pd.Series, side: Side, cfg: L2Config) -> float:
    bid_d = row.get("bid_depth", 0.0) or 0.0
    ask_d = row.get("ask_depth", 0.0) or 0.0
    total = bid_d + ask_d
    if total < cfg.min_book_depth:
        return 30.0
    ratio = bid_d / total if total else 0.5
    if side == Side.LONG:
        return _clamp(ratio * 100)
    return _clamp((1 - ratio) * 100)


def score_delta(row: pd.Series, side: Side) -> float:
    delta = row.get("delta", 0.0)
    if pd.isna(delta):
        return 50.0
    if side == Side.LONG:
        return _clamp(50 + delta / max(abs(delta), 1) * 30) if delta > 0 else 35.0
    return _clamp(50 + abs(delta) / max(abs(delta), 1) * 30) if delta < 0 else 35.0


def score_absorption(row: pd.Series, side: Side) -> float:
    """Proxy: tight spread + volume on pullback."""
    bar_range = row.get("bar_range", row["high"] - row["low"])
    atr_val = row.get("atr", bar_range)
    vol = row.get("volume", 0.0)
    if pd.isna(atr_val) or atr_val <= 0:
        return 50.0
    tight = bar_range < atr_val * 0.5
    high_vol = vol > row.get("volume", vol)  # placeholder; use relative in approximation
    base = 55.0 if tight else 40.0
    if side == Side.LONG and row["close"] >= row["open"]:
        base += 10
    if side == Side.SHORT and row["close"] <= row["open"]:
        base += 10
    if high_vol:
        base += 5
    return _clamp(base)


def approximate_l2_from_ohlcv(row: pd.Series, side: Side) -> dict[str, float]:
    """Approximate L2 metrics from OHLCV when book data is missing."""
    direction = 1.0 if row["close"] >= row["open"] else -1.0
    vol = float(row.get("volume", 100))
    bar_range = float(row["high"] - row["low"])
    imb = direction * min(bar_range / max(row.get("atr", bar_range), 0.01), 1.0) * 0.6
    bid_depth = vol * (0.55 if side == Side.LONG else 0.45)
    ask_depth = vol * (0.45 if side == Side.LONG else 0.55)
    return {
        "imbalance": imb,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "delta": direction * vol * 0.3,
        "bar_range": bar_range,
    }


def compute_l2_score(row: pd.Series, side: Side, cfg: L2Config) -> L2Score:
    approximated = False
    work = row
    if not _has_l2_columns(row):
        if not cfg.approximation_when_missing:
            return L2Score(score=0.0, approximated=False)
        approximated = True
        approx = approximate_l2_from_ohlcv(row, side)
        work = row.copy()
        for k, v in approx.items():
            work[k] = v
        if "imbalance" not in work.index or pd.isna(work.get("imbalance")):
            work["imbalance"] = approx["imbalance"]

    imb_s = score_imbalance(work, side, cfg)
    depth_s = score_depth(work, side, cfg)
    delta_s = score_delta(work, side)
    abs_s = score_absorption(work, side)

    total_w = cfg.weight_imbalance + cfg.weight_depth + cfg.weight_delta + cfg.weight_absorption
    score = (
        imb_s * cfg.weight_imbalance
        + depth_s * cfg.weight_depth
        + delta_s * cfg.weight_delta
        + abs_s * cfg.weight_absorption
    ) / total_w

    return L2Score(
        score=_clamp(score),
        components={"imbalance": imb_s, "depth": depth_s, "delta": delta_s, "absorption": abs_s},
        approximated=approximated,
    )


def l2_supports_bias(l2: L2Score, bias: Bias, min_score: float) -> bool:
    if bias == Bias.NONE:
        return False
    return l2.score >= min_score
