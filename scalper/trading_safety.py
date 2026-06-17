"""Safety gates for any live order path. Paper/research is the default."""

from __future__ import annotations

import os


LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_RISK"
DEMO_NT8_ACCOUNTS = frozenset({"SIM101", "DEMO2735784"})


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def paper_only_mode() -> bool:
    """True unless PAPER_ONLY is explicitly set to false."""
    return env_bool("PAPER_ONLY", default=True)


def _nt8_account_id() -> str:
    return os.getenv("NT8_ACCOUNT", "").split("!")[0].strip()


def _is_blocked_live_account(account: str) -> bool:
    """Block real Apex eval/live accounts — demo/sim only."""
    acct = account.upper()
    if acct in DEMO_NT8_ACCOUNTS:
        return False
    if acct.startswith("PA") and acct[2:3].isdigit():
        return True
    if acct.startswith("APEX-") and "DEMO" not in acct:
        return True
    return acct not in DEMO_NT8_ACCOUNTS


def demo_nt8_orders_enabled() -> bool:
    """NT8 orders on Sim101 / DEMO2735784 without LIVE_TRADING=true."""
    if not env_bool("NT8_DEMO_ORDERS", default=False):
        return False
    if env_bool("LIVE_TRADING", default=False):
        return False
    acct = _nt8_account_id()
    if not acct:
        return False
    if _is_blocked_live_account(acct):
        return False
    return acct.upper() in DEMO_NT8_ACCOUNTS


def live_trading_enabled() -> bool:
    """Live orders require LIVE_TRADING=true and explicit confirmation phrase."""
    if paper_only_mode():
        return False
    if not env_bool("LIVE_TRADING", default=False):
        return False
    confirm = os.getenv("LIVE_TRADING_CONFIRM", "").strip()
    return confirm == LIVE_CONFIRM_PHRASE


def nt8_orders_enabled() -> bool:
    """True when demo NT8 routing or fully confirmed live trading is enabled."""
    return demo_nt8_orders_enabled() or live_trading_enabled()


def assert_paper_mode(context: str = "operation") -> None:
    if nt8_orders_enabled():
        return
    # Paper mode is expected; no exception.


def require_live_trading(context: str) -> None:
    """Raise if live trading safety gates are not satisfied."""
    if demo_nt8_orders_enabled():
        return
    if paper_only_mode():
        raise RuntimeError(
            f"{context} blocked: PAPER_ONLY=true (default). "
            "Set NT8_DEMO_ORDERS=true with NT8_ACCOUNT=Sim101|DEMO2735784 for demo NT8 orders."
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
