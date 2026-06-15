"""Tests for sample data generators."""

from scalper.sample_data import generate_chop, generate_trend_down, generate_trend_up


def test_trend_up_columns() -> None:
    df = generate_trend_up(60)
    required = {"timestamp", "open", "high", "low", "close", "volume", "bid_size", "ask_size"}
    assert required.issubset(df.columns)
    assert len(df) == 60
    assert df["close"].iloc[-1] > df["close"].iloc[0]


def test_trend_down_declines() -> None:
    df = generate_trend_down(60)
    assert df["close"].iloc[-1] < df["close"].iloc[0]


def test_chop_low_directionality() -> None:
    df = generate_chop(60)
    net_move = abs(df["close"].iloc[-1] - df["close"].iloc[0])
    total_range = df["high"].max() - df["low"].min()
    assert net_move < total_range * 0.5
