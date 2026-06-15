"""Trend score (0-100) with LONG/SHORT/NONE bias."""

from __future__ import annotations

import pandas as pd

from scalper.config import TrendConfig
from scalper.models import Bias, TrendScore


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def score_ema_alignment(row: pd.Series, bias: Bias) -> float:
    if bias == Bias.LONG:
        if row["close"] > row["ema_fast"] > row["ema_slow"] > row["ema_trend"]:
            return 100.0
        if row["close"] > row["ema_fast"] > row["ema_slow"]:
            return 75.0
        if row["close"] > row["ema_fast"]:
            return 50.0
        return 20.0
    if bias == Bias.SHORT:
        if row["close"] < row["ema_fast"] < row["ema_slow"] < row["ema_trend"]:
            return 100.0
        if row["close"] < row["ema_fast"] < row["ema_slow"]:
            return 75.0
        if row["close"] < row["ema_fast"]:
            return 50.0
        return 20.0
    return 30.0


def score_vwap(row: pd.Series, bias: Bias) -> float:
    vwap = row.get("vwap", row["close"])
    dist = (row["close"] - vwap) / max(row.get("atr", 1.0), 0.01)
    if bias == Bias.LONG:
        return _clamp(50 + dist * 25)
    if bias == Bias.SHORT:
        return _clamp(50 - dist * 25)
    return 40.0


def score_adx(row: pd.Series, cfg: TrendConfig) -> float:
    adx_val = row.get("adx", 0.0)
    if pd.isna(adx_val):
        return 0.0
    if adx_val < cfg.adx_trend_min:
        return _clamp(adx_val / cfg.adx_trend_min * 50)
    return _clamp(50 + (adx_val - cfg.adx_trend_min) * 2.5)


def score_atr_expansion(row: pd.Series, prev_atr: float, cfg: TrendConfig) -> float:
    atr_val = row.get("atr", 0.0)
    if pd.isna(atr_val) or prev_atr <= 0:
        return 50.0
    ratio = atr_val / prev_atr
    if ratio >= cfg.atr_expansion_mult:
        return _clamp(60 + (ratio - 1) * 80)
    return _clamp(ratio / cfg.atr_expansion_mult * 50)


def score_structure(row: pd.Series, bias: Bias) -> float:
    if bias == Bias.LONG:
        hh = row.get("higher_high", 0.0)
        return 70.0 if hh else 40.0
    if bias == Bias.SHORT:
        ll = row.get("lower_low", 0.0)
        return 70.0 if ll else 40.0
    return 35.0


def detect_bias(row: pd.Series) -> Bias:
    if row["ema_fast"] > row["ema_slow"] and row["close"] > row["ema_slow"]:
        return Bias.LONG
    if row["ema_fast"] < row["ema_slow"] and row["close"] < row["ema_slow"]:
        return Bias.SHORT
    if row["ema_fast"] < row["ema_slow"] and row["close"] < row["ema_fast"]:
        return Bias.SHORT
    if row["ema_fast"] > row["ema_slow"] and row["close"] > row["ema_fast"]:
        return Bias.LONG
    return Bias.NONE


def compute_trend_score(row: pd.Series, prev_atr: float, cfg: TrendConfig) -> TrendScore:
    bias = detect_bias(row)
    if bias == Bias.NONE:
        return TrendScore(score=0.0, bias=Bias.NONE, components={})

    ema_s = score_ema_alignment(row, bias)
    vwap_s = score_vwap(row, bias)
    adx_s = score_adx(row, cfg)
    atr_s = score_atr_expansion(row, prev_atr, cfg)
    struct_s = score_structure(row, bias)

    total_weight = cfg.weight_ema + cfg.weight_vwap + cfg.weight_adx + cfg.weight_atr + cfg.weight_structure
    score = (
        ema_s * cfg.weight_ema
        + vwap_s * cfg.weight_vwap
        + adx_s * cfg.weight_adx
        + atr_s * cfg.weight_atr
        + struct_s * cfg.weight_structure
    ) / total_weight

    return TrendScore(
        score=_clamp(score),
        bias=bias,
        components={
            "ema": ema_s,
            "vwap": vwap_s,
            "adx": adx_s,
            "atr": atr_s,
            "structure": struct_s,
        },
    )
