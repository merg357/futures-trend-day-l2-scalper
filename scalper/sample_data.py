"""Synthetic sample data generators: trend up, trend down, chop."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


def _session_timestamps(n_bars: int, start: datetime | None = None) -> list[datetime]:
    start = start or datetime(2024, 6, 3, 9, 30)
    return [start + timedelta(minutes=i) for i in range(n_bars)]


def _base_ohlcv(
    n_bars: int,
    start_price: float,
    drift: float,
    noise: float,
    with_l2: bool = True,
    l2_bias: float = 0.0,
) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    prices = [start_price]
    rows = []
    ts_list = _session_timestamps(n_bars)

    for i in range(n_bars):
        ret = drift + rng.normal(0, noise)
        close = prices[-1] * (1 + ret)
        high = close + abs(rng.normal(0, noise * prices[-1] * 0.5))
        low = close - abs(rng.normal(0, noise * prices[-1] * 0.5))
        open_p = prices[-1]
        vol = int(rng.integers(200, 2000))
        prices.append(close)

        row: dict = {
            "timestamp": ts_list[i],
            "open": round(open_p, 2),
            "high": round(max(high, open_p, close), 2),
            "low": round(min(low, open_p, close), 2),
            "close": round(close, 2),
            "volume": vol,
        }
        if with_l2:
            imb = np.clip(l2_bias + rng.normal(0, 0.15), -1, 1)
            bid = vol * (0.5 + imb * 0.3)
            ask = vol * (0.5 - imb * 0.3)
            row["bid_size"] = round(bid, 1)
            row["ask_size"] = round(ask, 1)
            row["bid_depth"] = round(bid * 5, 1)
            row["ask_depth"] = round(ask * 5, 1)
            row["delta"] = round((close - open_p) / max(open_p, 1) * vol, 1)
        rows.append(row)

    return pd.DataFrame(rows)


def _inject_pullbacks(df: pd.DataFrame, tick_size: float = 0.25, side: str = "up") -> pd.DataFrame:
    """Post-process bars so pullbacks touch the fast EMA zone for entry logic."""
    from scalper.config import TrendConfig
    from scalper.indicators import compute_indicators

    out = df.copy()
    cfg = TrendConfig()
    tol = 3 * tick_size
    for _ in range(2):
        ind = compute_indicators(out, cfg)
        for i in range(cfg.ema_slow + 5, len(ind) - 1, 14):
            ema_f = float(ind.loc[i, "ema_fast"])
            if side == "up" and ind.loc[i, "ema_fast"] > ind.loc[i, "ema_slow"]:
                out.loc[i, "low"] = round(ema_f - tol, 2)
                out.loc[i, "close"] = round(ema_f + tol * 2, 2)
                out.loc[i, "open"] = round(ema_f + tol, 2)
                out.loc[i, "high"] = round(max(float(out.loc[i, "high"]), float(out.loc[i, "close"]) + 2), 2)
                out.loc[i, "bid_size"] = float(out.loc[i, "bid_size"]) * 1.5
                out.loc[i, "bid_depth"] = float(out.loc[i, "bid_depth"]) * 1.4
                out.loc[i, "delta"] = abs(float(out.loc[i, "delta"])) * 1.2
            elif side == "down" and ind.loc[i, "ema_fast"] < ind.loc[i, "ema_slow"]:
                out.loc[i, "high"] = round(ema_f + tol, 2)
                out.loc[i, "close"] = round(ema_f - tol * 2, 2)
                out.loc[i, "open"] = round(ema_f - tol, 2)
                out.loc[i, "low"] = round(min(float(out.loc[i, "low"]), float(out.loc[i, "close"]) - 2), 2)
                out.loc[i, "ask_size"] = float(out.loc[i, "ask_size"]) * 1.5
                out.loc[i, "ask_depth"] = float(out.loc[i, "ask_depth"]) * 1.4
                out.loc[i, "delta"] = -abs(float(out.loc[i, "delta"])) * 1.2
    return out


def generate_trend_up(n_bars: int = 120, symbol: str = "MNQ") -> pd.DataFrame:
    """Uptrend with pullbacks and bullish L2."""
    rng = np.random.default_rng(42)
    ts_list = _session_timestamps(n_bars)
    price = 18500.0
    rows = []
    for i in range(n_bars):
        price += rng.uniform(1.5, 4.0)
        open_p = price - rng.uniform(0, 1)
        close = price
        high = close + rng.uniform(0.5, 2)
        low = open_p - rng.uniform(0, 1)
        vol = int(rng.integers(400, 2500))
        imb = 0.35 + rng.normal(0, 0.1)
        rows.append({
            "timestamp": ts_list[i],
            "open": round(open_p, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": vol,
            "bid_size": round(vol * (0.55 + imb * 0.2), 1),
            "ask_size": round(vol * (0.45 - imb * 0.2), 1),
            "bid_depth": round(vol * 3.0, 1),
            "ask_depth": round(vol * 2.0, 1),
            "delta": round(vol * 0.25, 1),
        })
    df = pd.DataFrame(rows)
    df = _inject_pullbacks(df, side="up")
    df.attrs["symbol"] = symbol
    return df


def generate_trend_down(n_bars: int = 120, symbol: str = "MNQ") -> pd.DataFrame:
    """Downtrend with bearish L2."""
    rng = np.random.default_rng(43)
    ts_list = _session_timestamps(n_bars)
    price = 18500.0
    rows = []
    for i in range(n_bars):
        price -= rng.uniform(1.5, 4.0)
        open_p = price + rng.uniform(0, 1)
        close = price
        low = close - rng.uniform(0.5, 2)
        high = open_p + rng.uniform(0, 1)
        vol = int(rng.integers(400, 2500))
        imb = -0.35 + rng.normal(0, 0.1)
        rows.append({
            "timestamp": ts_list[i],
            "open": round(open_p, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": vol,
            "bid_size": round(vol * (0.45 - imb * 0.2), 1),
            "ask_size": round(vol * (0.55 + imb * 0.2), 1),
            "bid_depth": round(vol * 2.0, 1),
            "ask_depth": round(vol * 3.0, 1),
            "delta": round(-vol * 0.25, 1),
        })
    df = pd.DataFrame(rows)
    df = _inject_pullbacks(df, side="down")
    df.attrs["symbol"] = symbol
    return df


def generate_chop(n_bars: int = 120, symbol: str = "MNQ") -> pd.DataFrame:
    """Choppy/range-bound session — low ADX, no clear trend."""
    rng = np.random.default_rng(99)
    ts_list = _session_timestamps(n_bars)
    center = 18500.0
    rows = []
    for i in range(n_bars):
        wobble = center + rng.normal(0, 15)
        open_p = wobble + rng.normal(0, 3)
        close = wobble + rng.normal(0, 3)
        high = max(open_p, close) + abs(rng.normal(0, 2))
        low = min(open_p, close) - abs(rng.normal(0, 2))
        vol = int(rng.integers(150, 800))
        imb = rng.normal(0, 0.1)
        rows.append({
            "timestamp": ts_list[i],
            "open": round(open_p, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": vol,
            "bid_size": round(vol * 0.5, 1),
            "ask_size": round(vol * 0.5, 1),
            "bid_depth": round(vol * 2.5, 1),
            "ask_depth": round(vol * 2.5, 1),
            "delta": round(rng.normal(0, vol * 0.1), 1),
        })
    df = pd.DataFrame(rows)
    df.attrs["symbol"] = symbol
    return df


def generate_all_samples(out_dir: str | Path, n_bars: int = 120) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    datasets = {
        "mnq_trend_up.csv": generate_trend_up(n_bars, "MNQ"),
        "mnq_trend_down.csv": generate_trend_down(n_bars, "MNQ"),
        "mnq_chop.csv": generate_chop(n_bars, "MNQ"),
        "mes_trend_up.csv": generate_trend_up(n_bars, "MES"),
        "mes_chop.csv": generate_chop(n_bars, "MES"),
    }
    paths = {}
    for name, df in datasets.items():
        p = out / name
        df.to_csv(p, index=False)
        paths[name] = p
    return paths
