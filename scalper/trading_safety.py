"""Safety gates for any live order path. Paper/research is the default."""

from __future__ import annotations

import os


LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_RISK"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def paper_only_mode() -> bool:
    """True unless PAPER_ONLY is explicitly set to false."""
    return env_bool("PAPER_ONLY", default=True)


def live_trading_enabled() -> bool:
    """Live orders require LIVE_TRADING=true and explicit confirmation phrase."""
    if paper_only_mode():
        return False
    if not env_bool("LIVE_TRADING", default=False):
        return False
    confirm = os.getenv("LIVE_TRADING_CONFIRM", "").strip()
    return confirm == LIVE_CONFIRM_PHRASE


def assert_paper_mode(context: str = "operation") -> None:
    if live_trading_enabled():
        return
    # Paper mode is expected; no exception.


def require_live_trading(context: str) -> None:
    """Raise if live trading safety gates are not satisfied."""
    if paper_only_mode():
        raise RuntimeError(
            f"{context} blocked: PAPER_ONLY=true (default). "
            "Set PAPER_ONLY=false only after independent validation."
        )
    if not env_bool("LIVE_TRADING", default=False):
        raise RuntimeError(
            f"{context} blocked: LIVE_TRADING is not true. "
            "This project defaults to research/paper mode."
        )
    confirm = os.getenv("LIVE_TRADING_CONFIRM", "").strip()
    if confirm != LIVE_CONFIRM_PHRASE:
        raise RuntimeError(
            f"{context} blocked: set LIVE_TRADING_CONFIRM={LIVE_CONFIRM_PHRASE} "
            "in the environment after explicit human review."
        )
