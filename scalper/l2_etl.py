"""Convert Rithmic L2 tar.gz archives to scalper CSV bars (1m and sub-minute)."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

FOTICKLAB_SRC = Path(r"D:\AI_Vault\futures-options-backtest-lab\src")
if FOTICKLAB_SRC.is_dir() and str(FOTICKLAB_SRC) not in sys.path:
    sys.path.insert(0, str(FOTICKLAB_SRC))

try:
    from foticklab.trade_data import collect_tar_archives, resolve_archive_paths
except ImportError:  # pragma: no cover - fallback when foticklab unavailable
    collect_tar_archives = None  # type: ignore[assignment,misc]
    resolve_archive_paths = None  # type: ignore[assignment,misc]

DEFAULT_ARCHIVE_ROOT = Path(r"D:\TradeData\StorageBox\bundles\futuresbot\archives\l2")
TRADE_COLUMNS = [
    "ts_ns",
    "price",
    "size",
    "aggressor",
    "bid_px_00",
    "bid_sz_00",
    "ask_px_00",
    "ask_sz_00",
]
DEPTH_COLUMNS = ["ts_ns", "side", "price", "size", "level_position", "operation"]

INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "30s": 30,
    "15s": 15,
    "10s": 10,
}


@dataclass
class BarAgg:
    prices: list[float] = field(default_factory=list)
    sizes: list[float] = field(default_factory=list)
    aggressors: list[str] = field(default_factory=list)
    bid_px: float | None = None
    ask_px: float | None = None
    bid_sz: float | None = None
    ask_sz: float | None = None
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    depth_bid_px: float | None = None
    depth_ask_px: float | None = None
    depth_bid_sz: float | None = None
    depth_ask_sz: float | None = None


@dataclass
class ConvertResult:
    instrument: str
    date: str
    interval: str
    archive_path: str
    output_path: str
    row_count: int
    start_timestamp: str
    end_timestamp: str
    l2_real: bool
    depth_levels_available: int
    depth_levels_approximated: int
    trade_files_read: int
    depth_files_read: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "date": self.date,
            "interval": self.interval,
            "archive_path": self.archive_path,
            "output_path": self.output_path,
            "row_count": self.row_count,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "l2_real": self.l2_real,
            "depth_levels_available": self.depth_levels_available,
            "depth_levels_approximated": self.depth_levels_approximated,
            "trade_files_read": self.trade_files_read,
            "depth_files_read": self.depth_files_read,
            "warnings": self.warnings,
        }


def parse_interval(interval: str) -> tuple[str, int]:
    key = interval.lower().strip()
    if key not in INTERVAL_SECONDS:
        supported = ", ".join(sorted(INTERVAL_SECONDS))
        raise ValueError(f"Unsupported interval '{interval}'. Use one of: {supported}")
    return key, INTERVAL_SECONDS[key]


def default_output_dir(interval: str, base_dir: Path | None = None) -> Path:
    root = base_dir or Path("data/raw")
    if interval == "1m":
        return root
    return root / "submin"


def output_filename(instrument: str, date: str, interval: str) -> str:
    return f"{instrument.upper()}_{date}_{interval}.csv"


def resolve_archive(
    instrument: str,
    date: str,
    archive: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> Path:
    if archive is not None:
        path = Path(archive)
        if not path.is_file():
            raise FileNotFoundError(f"Archive not found: {path}")
        return path
    if collect_tar_archives is not None:
        matches = collect_tar_archives("l2", instrument.upper(), (date,))
        return matches[0]
    candidates: list[Path] = [DEFAULT_ARCHIVE_ROOT / instrument.upper() / f"{date}.tar.gz"]
    for root in extra_roots or []:
        candidates.append(root / instrument.upper() / f"{date}.tar.gz")
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f"No archive for {instrument} {date}: tried {candidates}")


def _to_bar_bucket(ts_ns: pd.Series, interval_seconds: int, tz: str = "America/New_York") -> pd.Series:
    ts = pd.to_datetime(ts_ns.astype("int64"), utc=True, unit="ns").dt.tz_convert(tz)
    return ts.dt.floor(f"{interval_seconds}s")


def _in_rth(bar_ts: pd.Timestamp, open_time: str = "09:30", close_time: str = "16:00") -> bool:
    if bar_ts.tzinfo is None:
        return True
    t = bar_ts.time()
    open_h, open_m = map(int, open_time.split(":"))
    close_h, close_m = map(int, close_time.split(":"))
    from datetime import time

    return time(open_h, open_m) <= t < time(close_h, close_m)


def _in_session(
    bar_ts: pd.Timestamp,
    session_filter: str = "rth",
    open_time: str = "09:30",
    close_time: str = "16:00",
) -> bool:
    """Filter bars by session window. 'all' keeps every bucket; 'globex' excludes RTH-only gap logic."""
    if session_filter == "all":
        return True
    if session_filter == "globex":
        if bar_ts.tzinfo is None:
            return True
        t = bar_ts.time()
        from datetime import time

        open_h, open_m = map(int, open_time.split(":"))
        close_h, close_m = map(int, close_time.split(":"))
        rth_open = time(open_h, open_m)
        rth_close = time(close_h, close_m)
        # Globex: overnight + RTH (exclude only the daily maintenance window ~17:00-18:00 ET).
        maint_start = time(17, 0)
        maint_end = time(18, 0)
        if maint_start <= t < maint_end:
            return False
        if rth_open <= t < rth_close:
            return True
        return t >= time(18, 0) or t < rth_open
    return _in_rth(bar_ts, open_time, close_time)


def _read_parquet_member(tar: tarfile.TarFile, member: tarfile.TarInfo, columns: list[str]) -> pd.DataFrame:
    import pyarrow.parquet as pq

    handle = tar.extractfile(member)
    if handle is None:
        return pd.DataFrame()
    table = pq.read_table(io.BytesIO(handle.read()), columns=columns)
    return table.to_pandas()


def _accumulate_trades(
    archive: Path,
    buckets: dict[pd.Timestamp, BarAgg],
    tz: str,
    interval_seconds: int,
    session_filter: str = "rth",
) -> tuple[int, list[str]]:
    warnings: list[str] = []
    files_read = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = [
            m
            for m in tar.getmembers()
            if m.isfile()
            and "/trades_" in m.name
            and m.name.endswith(".parquet")
            and ".tmp.parquet" not in m.name
        ]
        for member in sorted(members, key=lambda item: item.name):
            try:
                frame = _read_parquet_member(tar, member, TRADE_COLUMNS)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Skipped trade member {member.name}: {exc}")
                continue
            if frame.empty:
                continue
            files_read += 1
            frame = frame.copy()
            frame["bar_bucket"] = _to_bar_bucket(frame["ts_ns"], interval_seconds, tz)
            frame = frame[frame["bar_bucket"].map(lambda ts: _in_session(ts, session_filter))]
            if frame.empty:
                continue
            frame["aggressor"] = frame["aggressor"].astype(str).str.lower()
            for bar_ts, group in frame.groupby("bar_bucket", sort=False):
                bucket = buckets[bar_ts]
                bucket.prices.extend(group["price"].astype(float).tolist())
                bucket.sizes.extend(group["size"].fillna(0).astype(float).tolist())
                bucket.aggressors.extend(group["aggressor"].tolist())
                bucket.buy_vol += float(group.loc[group["aggressor"] == "buy", "size"].fillna(0).sum())
                bucket.sell_vol += float(group.loc[group["aggressor"] == "sell", "size"].fillna(0).sum())
                last = group.iloc[-1]
                for attr, col in (
                    ("bid_px", "bid_px_00"),
                    ("ask_px", "ask_px_00"),
                    ("bid_sz", "bid_sz_00"),
                    ("ask_sz", "ask_sz_00"),
                ):
                    val = last.get(col)
                    if pd.notna(val):
                        setattr(bucket, attr, float(val))
    return files_read, warnings


def _accumulate_depth(
    archive: Path,
    buckets: dict[pd.Timestamp, BarAgg],
    tz: str,
    interval_seconds: int,
    max_level_seen: list[int],
    session_filter: str = "rth",
) -> tuple[int, list[str]]:
    warnings: list[str] = []
    files_read = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = [
            m
            for m in tar.getmembers()
            if m.isfile()
            and "/depth_updates_" in m.name
            and m.name.endswith(".parquet")
            and ".tmp.parquet" not in m.name
        ]
        for member in sorted(members, key=lambda item: item.name):
            try:
                frame = _read_parquet_member(tar, member, DEPTH_COLUMNS)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Skipped depth member {member.name}: {exc}")
                continue
            if frame.empty:
                continue
            files_read += 1
            frame = frame[(frame["level_position"] == 0)]
            if frame.empty:
                continue
            max_level_seen[0] = max(max_level_seen[0], 0)
            frame = frame.copy()
            frame["bar_bucket"] = _to_bar_bucket(frame["ts_ns"], interval_seconds, tz)
            frame = frame[frame["bar_bucket"].map(lambda ts: _in_session(ts, session_filter))]
            if frame.empty:
                continue
            frame["side"] = frame["side"].astype(str).str.lower()
            for bar_ts, group in frame.groupby("bar_bucket", sort=False):
                bucket = buckets[bar_ts]
                last_bid = group[group["side"].str.startswith("bid")].tail(1)
                last_ask = group[group["side"].str.startswith("ask")].tail(1)
                if not last_bid.empty:
                    bucket.depth_bid_px = float(last_bid.iloc[-1]["price"])
                    bucket.depth_bid_sz = float(last_bid.iloc[-1]["size"])
                if not last_ask.empty:
                    bucket.depth_ask_px = float(last_ask.iloc[-1]["price"])
                    bucket.depth_ask_sz = float(last_ask.iloc[-1]["size"])
    return files_read, warnings


def _finalize_buckets(buckets: dict[pd.Timestamp, BarAgg]) -> pd.DataFrame:
    if not buckets:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    cum_delta = 0.0
    for bar_ts in sorted(buckets):
        b = buckets[bar_ts]
        bar_delta = b.buy_vol - b.sell_vol
        cum_delta += bar_delta

        if b.prices:
            open_p = b.prices[0]
            high_p = max(b.prices)
            low_p = min(b.prices)
            close_p = b.prices[-1]
            volume = float(sum(b.sizes))
            trade_price = b.prices[-1]
            trade_size = b.sizes[-1]
            trade_side = b.aggressors[-1] if b.aggressors else ""
        else:
            mid_candidates = [
                x
                for x in (
                    (b.depth_bid_px, b.depth_ask_px),
                    (b.bid_px, b.ask_px),
                )
                if x[0] is not None and x[1] is not None and x[0] <= x[1]
            ]
            if not mid_candidates:
                continue
            bid, ask = mid_candidates[-1]
            close_p = (bid + ask) / 2.0
            open_p = high_p = low_p = close_p
            volume = 0.0
            trade_price = close_p
            trade_size = 0.0
            trade_side = ""

        bid = b.bid_px if b.bid_px is not None else b.depth_bid_px
        ask = b.ask_px if b.ask_px is not None else b.depth_ask_px
        bid_size = b.bid_sz if b.bid_sz is not None else b.depth_bid_sz
        ask_size = b.ask_sz if b.ask_sz is not None else b.depth_ask_sz
        if bid is None or ask is None:
            bid = close_p - 0.25
            ask = close_p + 0.25
        if bid_size is None:
            bid_size = 0.0
        if ask_size is None:
            ask_size = 0.0

        ts_out = bar_ts
        if hasattr(bar_ts, "tzinfo") and bar_ts.tzinfo is not None:
            ts_out = bar_ts.tz_convert("America/New_York").tz_localize(None)

        row = {
            "timestamp": ts_out,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": int(round(volume)),
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "bid_size": round(float(bid_size), 1),
            "ask_size": round(float(ask_size), 1),
            "bid_depth_1": round(float(bid_size), 1),
            "bid_depth_2": 0.0,
            "bid_depth_3": 0.0,
            "bid_depth_4": 0.0,
            "bid_depth_5": 0.0,
            "ask_depth_1": round(float(ask_size), 1),
            "ask_depth_2": 0.0,
            "ask_depth_3": 0.0,
            "ask_depth_4": 0.0,
            "ask_depth_5": 0.0,
            "bid_depth": round(float(bid_size), 1),
            "ask_depth": round(float(ask_size), 1),
            "trade_price": round(trade_price, 2),
            "trade_size": round(float(trade_size), 1),
            "trade_side": trade_side,
            "delta": round(cum_delta, 1),
            "bar_delta": round(bar_delta, 1),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("timestamp").reset_index(drop=True)


def convert_archive_to_bars(
    instrument: str,
    date: str,
    *,
    archive: Path | None = None,
    output_dir: Path | None = None,
    timezone: str = "America/New_York",
    include_depth: bool = False,
    interval: str = "1m",
    session_filter: str = "rth",
    extra_archive_roots: list[Path] | None = None,
) -> ConvertResult:
    interval_key, interval_seconds = parse_interval(interval)
    archive_path = resolve_archive(instrument, date, archive, extra_roots=extra_archive_roots)
    out_dir = output_dir or default_output_dir(interval_key)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / output_filename(instrument, date, interval_key)

    buckets: dict[pd.Timestamp, BarAgg] = defaultdict(BarAgg)
    max_level_seen = [0]
    warnings: list[str] = []

    trade_files, trade_warn = _accumulate_trades(
        archive_path, buckets, timezone, interval_seconds, session_filter=session_filter
    )
    warnings.extend(trade_warn)
    depth_files = 0
    if include_depth:
        depth_files, depth_warn = _accumulate_depth(
            archive_path, buckets, timezone, interval_seconds, max_level_seen, session_filter=session_filter
        )
        warnings.extend(depth_warn)
    else:
        warnings.append("Depth pass skipped; book state from trade snapshots (bid_sz_00/ask_sz_00).")

    df = _finalize_buckets(buckets)
    if df.empty:
        raise ValueError(
            f"No {session_filter} {interval_key} bars produced for {instrument} {date} from {archive_path}"
        )

    df.to_csv(output_path, index=False)

    depth_available = min(max(max_level_seen[0] + 1, 1), 5)
    depth_approx = max(0, 5 - depth_available)
    l2_real = bool((df[["bid_size", "ask_size"]].fillna(0) > 0).any().any())

    return ConvertResult(
        instrument=instrument.upper(),
        date=date,
        interval=interval_key,
        archive_path=str(archive_path),
        output_path=str(output_path),
        row_count=len(df),
        start_timestamp=str(df["timestamp"].iloc[0]),
        end_timestamp=str(df["timestamp"].iloc[-1]),
        l2_real=l2_real,
        depth_levels_available=depth_available,
        depth_levels_approximated=depth_approx,
        trade_files_read=trade_files,
        depth_files_read=depth_files,
        warnings=warnings[:20],
    )


def write_manifest(results: list[ConvertResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"files": [r.to_dict() for r in results]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
