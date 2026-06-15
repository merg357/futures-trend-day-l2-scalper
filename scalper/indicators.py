"""Technical and L2-derived indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    typical = (high + low + close) / 3.0
    cum_vol = volume.cumsum()
    cum_tp_vol = (typical * volume).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(high, low, close)
    atr_val = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def rolling_delta(close: pd.Series, volume: pd.Series, window: int = 5) -> pd.Series:
    """Approximate delta from price direction × volume."""
    direction = np.sign(close.diff().fillna(0))
    signed_vol = direction * volume
    return signed_vol.rolling(window, min_periods=1).sum()


def bid_ask_imbalance(bid_size: pd.Series, ask_size: pd.Series) -> pd.Series:
    total = bid_size + ask_size
    return (bid_size - ask_size) / total.replace(0, np.nan)


def book_depth_ratio(bid_depth: pd.Series, ask_depth: pd.Series) -> pd.Series:
    total = bid_depth + ask_depth
    return bid_depth / total.replace(0, np.nan)


def higher_highs_lows(high: pd.Series, low: pd.Series, window: int = 5) -> tuple[pd.Series, pd.Series]:
    hh = (high == high.rolling(window, min_periods=window).max()).astype(float)
    ll = (low == low.rolling(window, min_periods=window).min()).astype(float)
    return hh, ll


def compute_indicators(df: pd.DataFrame, config_trend: object) -> pd.DataFrame:
    """Add all indicator columns to a DataFrame in-place copy."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], config_trend.ema_fast)
    out["ema_slow"] = ema(out["close"], config_trend.ema_slow)
    out["ema_trend"] = ema(out["close"], config_trend.ema_trend)
    if config_trend.vwap_enabled:
        out["vwap"] = vwap(out["high"], out["low"], out["close"], out["volume"])
    else:
        out["vwap"] = out["close"]
    out["atr"] = atr(out["high"], out["low"], out["close"], config_trend.atr_period)
    out["adx"] = adx(out["high"], out["low"], out["close"], config_trend.adx_period)
    if "delta" in out.columns and out["delta"].notna().any():
        pass  # preserve ETL cumulative delta from real L2 trades
    else:
        out["delta"] = rolling_delta(out["close"], out["volume"])
    out["bar_range"] = out["high"] - out["low"]

    if "bid_size" in out.columns and "ask_size" in out.columns:
        out["imbalance"] = bid_ask_imbalance(out["bid_size"], out["ask_size"])
    else:
        out["imbalance"] = np.nan

    if "bid_depth" in out.columns and "ask_depth" in out.columns:
        out["depth_ratio"] = book_depth_ratio(out["bid_depth"], out["ask_depth"])
    else:
        out["depth_ratio"] = np.nan

    hh, ll = higher_highs_lows(out["high"], out["low"])
    out["higher_high"] = hh
    out["lower_low"] = ll
    return out
