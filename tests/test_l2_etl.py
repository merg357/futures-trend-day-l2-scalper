"""Tests for L2 ETL interval support."""

from __future__ import annotations

import pytest

from scalper.l2_etl import INTERVAL_SECONDS, default_output_dir, output_filename, parse_interval


def test_parse_interval_supported():
    assert parse_interval("10s") == ("10s", 10)
    assert parse_interval("15S") == ("15s", 15)
    assert parse_interval("30s") == ("30s", 30)
    assert parse_interval("1m") == ("1m", 60)


def test_parse_interval_invalid():
    with pytest.raises(ValueError, match="Unsupported interval"):
        parse_interval("5s")


def test_output_filename():
    assert output_filename("mnq", "20260501", "10s") == "MNQ_20260501_10s.csv"


def test_default_output_dir():
    assert default_output_dir("1m").name == "raw" or str(default_output_dir("1m")).endswith("raw")
    assert default_output_dir("10s").name == "submin"
