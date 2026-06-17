"""RTH session window helpers shared by entry and exit rules."""

from __future__ import annotations

from datetime import datetime, time

from scalper.config import ScalperConfig

OUTSIDE_RTH = "outside_rth"


def parse_session_time(t: str) -> time:
    h, m = t.split(":")
    return time(int(h), int(m))


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def is_rth(bar_time: datetime, config: ScalperConfig) -> bool:
    """True when bar time is within [rth_open, rth_close) ET."""
    open_t = parse_session_time(config.session.rth_open)
    close_t = parse_session_time(config.session.rth_close)
    bar_minutes = _minutes(bar_time.time())
    return _minutes(open_t) <= bar_minutes < _minutes(close_t)


def is_session_end(bar_time: datetime, config: ScalperConfig) -> bool:
    """True only during flatten window [rth_close - flatten_min, rth_close] ET."""
    if not config.exit.exit_at_session_end:
        return False
    close_t = parse_session_time(config.session.rth_close)
    flatten_min = config.session.flatten_before_close_minutes
    flatten_start = _minutes(close_t) - flatten_min
    close_minutes = _minutes(close_t)
    bar_minutes = _minutes(bar_time.time())
    return flatten_start <= bar_minutes <= close_minutes


def rth_entry_block_reason(bar_time: datetime, config: ScalperConfig) -> str | None:
    if config.entry.rth_only and not is_rth(bar_time, config):
        return OUTSIDE_RTH
    return None
