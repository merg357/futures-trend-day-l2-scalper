"""Live broker gateway — NT8 demo/sim orders when NT8_DEMO_ORDERS is enabled."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from scalper.models import Side
from scalper.trading_safety import demo_nt8_orders_enabled, nt8_orders_enabled, require_live_trading

logger = logging.getLogger(__name__)

_NT8_DLL = r"C:\Program Files\NinjaTrader 8\bin\NinjaTrader.Client.dll"
_NT8_HOST = os.getenv("NT8_CLIENT_HOST", "127.0.0.1")
_NT8_PORT = int(os.getenv("NT8_CLIENT_PORT", "36973"))


class OrderAction(str, Enum):
    ENTER = "enter"
    EXIT = "exit"
    FLATTEN = "flatten"


@dataclass
class OrderRequest:
    action: OrderAction
    symbol: str
    side: Side
    quantity: int
    price: float | None = None
    reason: str = ""


def _resolve_instrument(symbol: str) -> str:
    explicit = os.getenv("NT8_INSTRUMENT", "").strip()
    if explicit:
        return explicit
    root = str(symbol or "MNQ").upper()[:3]
    month_map = {
        "F": "01", "G": "02", "H": "03", "J": "04", "K": "05", "M": "06",
        "N": "07", "Q": "08", "U": "09", "V": "10", "X": "11", "Z": "12",
    }
    # Default front month from env or June 2026 contract on this VPS.
    override = os.getenv("NT8_FRONT_MONTH", "").strip()
    if override:
        return override
    return f"{root} 06-26"


def _nt8_account() -> str:
    return os.getenv("NT8_ACCOUNT", "DEMO2735784").split("!")[0].strip()


def _order_prefix() -> str:
    return os.getenv("NT8_ORDER_PREFIX", "L2SCALP").strip() or "L2SCALP"


def _order_prefix_tag() -> str:
    raw = _order_prefix()
    return raw if raw.endswith("_") else f"{raw}_"


def _enforce_bot_order_id(order_id: str) -> str:
    """All bot NT8 orders must use L2SCALP_* (or NT8_ORDER_PREFIX_*) ids."""
    oid = str(order_id or "").strip()
    tag = _order_prefix_tag()
    if oid.upper().startswith(tag.upper()):
        return oid
    raise ValueError(f"refusing non-bot order id (expected {tag}*): {oid!r}")


def is_entry_blocked(payload: dict[str, Any]) -> bool:
    """True when NT8 routing was attempted but entry did not go live."""
    status = str(payload.get("status", ""))
    if status == "submitted":
        return False
    if status == "blocked_paper_mode":
        return False
    if status.startswith("blocked") or status in {"nt8_rejected", "order_failed"}:
        return True
    return status not in {"", "unsupported_action"} and status != "submitted"


def _query_nt8_market_position(account: str, instrument: str) -> int | None:
    """Return signed NT8 MarketPosition (0=flat) or None when API unavailable."""
    ps_cmd = (
        f'$asm = [System.Reflection.Assembly]::LoadFile("{_NT8_DLL}"); '
        f'$c = [Activator]::CreateInstance($asm.GetType("NinjaTrader.Client.Client")); '
        f'$c.SetUp("{_NT8_HOST}", {_NT8_PORT}); '
        f'Start-Sleep -Milliseconds 500; '
        f'$pos = $c.MarketPosition("{instrument}", "{account}"); '
        f'$c.TearDown(); '
        f'Write-Output $pos'
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return None
        for line in reversed(result.stdout.splitlines()):
            line = line.strip()
            if line.lstrip("-").isdigit():
                return int(line)
        return None
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        logger.warning("NT8 MarketPosition query failed: %s", exc)
        return None


def _market_sanity_block(account: str, symbol: str, signal_price: float) -> tuple[bool, str]:
    """Reuse FuturesBot nt8_market_sanity when available on this VPS."""
    try:
        import sys

        fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
        if fb_root not in sys.path:
            sys.path.insert(0, fb_root)
        from nt8_market_sanity import (
            divergence_alert_active,
            entries_halted,
            should_block_nt8_market_order,
        )

        root = str(symbol or "MNQ").upper()[:3]
        acct = account.split("!")[0]
        blocked, reason = should_block_nt8_market_order(
            root=root,
            account=acct,
            signal_price=signal_price,
            bot="l2_scalper_paper_runner",
        )
        if blocked:
            return True, reason
        if divergence_alert_active(account=acct):
            halted, reason = entries_halted(acct, root=root, bot="l2_scalper_paper_runner")
            if halted:
                return True, reason
        halted, reason = entries_halted(acct, root=root, bot="l2_scalper_paper_runner")
        return halted, reason
    except Exception as exc:
        logger.warning("market sanity check unavailable: %s", exc)
        return True, f"market sanity unavailable: {exc}"


def _nt8_client_command(
    command: str,
    account: str,
    instrument: str,
    action: str,
    qty: int,
    order_type: str,
    order_id: str,
    limit_price: float = 0.0,
) -> bool:
    ps_cmd = (
        f'$asm = [System.Reflection.Assembly]::LoadFile("{_NT8_DLL}"); '
        f'$c = [Activator]::CreateInstance($asm.GetType("NinjaTrader.Client.Client")); '
        f'$c.SetUp("{_NT8_HOST}", {_NT8_PORT}); '
        f'Start-Sleep -Milliseconds 500; '
        f'$c.ConfirmOrders(0); '
        f'$r = $c.Command("{command}", "{account}", "{instrument}", "{action}", '
        f'{qty}, "{order_type}", {limit_price}, 0.0, "DAY", "", "{order_id}", "", ""); '
        f'$c.TearDown(); '
        f'exit $r'
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = result.returncode == 0
        if not ok:
            logger.error(
                "NT8 %s %s failed rc=%s out=%s",
                command,
                instrument,
                result.returncode,
                (result.stdout + result.stderr).strip()[:240],
            )
        return ok
    except subprocess.TimeoutExpired:
        logger.error("NT8 command timeout: %s %s", command, instrument)
        return False
    except Exception as exc:
        logger.error("NT8 command error: %s", exc)
        return False


class LiveGateway:
    """Broker adapter — sends NT8 demo orders when NT8_DEMO_ORDERS=true."""

    def __init__(self, log_dir: str | Path = "data/live") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._audit_path = self.log_dir / "gateway_audit.jsonl"
        self._connected = False
        self._order_seq = 0
        self._account = _nt8_account()
        self._instrument = _resolve_instrument(os.getenv("SCALPER_SYMBOL", "MNQ"))

    def connect(self) -> bool:
        self._connected = True
        self._audit(
            "connect",
            {
                "status": "connected",
                "nt8_orders": nt8_orders_enabled(),
                "demo_nt8": demo_nt8_orders_enabled(),
                "account": self._account,
                "instrument": self._instrument,
            },
        )
        logger.info(
            "LiveGateway connected account=%s instrument=%s demo_nt8=%s",
            self._account,
            self._instrument,
            demo_nt8_orders_enabled(),
        )
        return True

    def disconnect(self) -> None:
        self._connected = False
        self._audit("disconnect", {})

    @property
    def connected(self) -> bool:
        return self._connected

    def query_market_position(self) -> int | None:
        """Signed NT8 position for configured account/instrument (0=flat)."""
        if not nt8_orders_enabled():
            return 0
        return _query_nt8_market_position(self._account, self._instrument)

    def _next_order_id(self, tag: str) -> str:
        self._order_seq += 1
        return _enforce_bot_order_id(
            f"{_order_prefix_tag()}{tag}_{int(datetime.now().timestamp())}_{self._order_seq}"
        )

    def cancel_orphan_orders(
        self,
        *,
        reason: str = "startup",
        before_entry: bool = False,
    ) -> dict[str, Any]:
        """Cancel non-L2SCALP_* MNQ working orders (or all MNQ working before entry)."""
        try:
            import sys

            fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
            if fb_root not in sys.path:
                sys.path.insert(0, fb_root)
            from l2_nt8_orphan_guard import cancel_orphan_mnq_orders

            report = cancel_orphan_mnq_orders(
                account=self._account,
                instrument=self._instrument,
                reason=reason,
                cancel_all_mnq_before_entry=before_entry,
                audit_path=self._audit_path,
            )
            if report.get("cancelled") or report.get("cancel_failed"):
                logger.info(
                    "orphan_guard %s: working=%s cancelled=%s failed=%s remaining=%s",
                    reason,
                    report.get("working_orders_found"),
                    len(report.get("cancelled") or []),
                    len(report.get("cancel_failed") or []),
                    report.get("orphan_remaining"),
                )
            return report
        except Exception as exc:
            payload = {
                "ok": False,
                "reason": reason,
                "error": str(exc),
                "account": self._account,
                "instrument": self._instrument,
            }
            self._audit("orphan_guard_error", payload)
            logger.warning("orphan_guard failed (%s): %s", reason, exc)
            return payload

    def cancel_order(self, order_id: str, *, reason: str = "") -> dict[str, Any]:
        """Cancel one NT8 working order by bot order id."""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "order_id": order_id,
            "reason": reason,
            "account": self._account,
            "instrument": self._instrument,
        }
        if not nt8_orders_enabled():
            payload["status"] = "blocked_paper_mode"
            self._audit("cancel_blocked", payload)
            return payload
        try:
            oid = _enforce_bot_order_id(order_id)
        except ValueError as exc:
            payload["status"] = "invalid_order_id"
            payload["error"] = str(exc)
            self._audit("cancel_failed", payload)
            return payload
        try:
            import sys

            fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
            if fb_root not in sys.path:
                sys.path.insert(0, fb_root)
            from l2_nt8_orphan_guard import _cancel_nt8_order

            ok = _cancel_nt8_order(self._account, self._instrument, oid)
        except Exception as exc:
            payload["status"] = "cancel_error"
            payload["error"] = str(exc)
            self._audit("cancel_failed", payload)
            logger.warning("cancel_order failed id=%s: %s", oid, exc)
            return payload
        payload["status"] = "cancelled" if ok else "cancel_failed"
        self._audit("order_cancelled" if ok else "cancel_failed", payload)
        if ok:
            logger.info("NT8 cancel OK: id=%s reason=%s", oid, reason)
        return payload

    def submit_order(self, request: OrderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": request.action.value,
            "symbol": request.symbol,
            "side": request.side.value,
            "quantity": request.quantity,
            "price": request.price,
            "reason": request.reason,
            "nt8_orders_enabled": nt8_orders_enabled(),
            "demo_nt8": demo_nt8_orders_enabled(),
            "account": self._account,
            "instrument": self._instrument,
        }
        if not nt8_orders_enabled():
            payload["status"] = "blocked_paper_mode"
            self._audit("order_blocked", payload)
            logger.warning("Order blocked (no NT8 demo/live gate): %s", request.reason)
            return payload

        require_live_trading("submit_order")
        qty = max(1, int(request.quantity or 1))
        tag = request.reason.replace(" ", "_")[:24] if request.reason else request.action.value

        entry_type, limit_px = "MARKET", 0.0
        if request.action == OrderAction.ENTER:
            self.cancel_orphan_orders(reason="pre_entry", before_entry=True)
            nt8_action = "BUY" if request.side == Side.LONG else "SELLSHORT"
            order_id = self._next_order_id(f"ENT_{tag}")
            try:
                import sys

                fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
                if fb_root not in sys.path:
                    sys.path.insert(0, fb_root)
                from nt8_market_sanity import demo_entry_order_spec

                entry_type, limit_px = demo_entry_order_spec(
                    str(request.symbol or "MNQ"),
                    float(request.price or 0) or None,
                    side=request.side.value,
                )
            except Exception:
                entry_type, limit_px = "MARKET", 0.0
            sanity_px = float(limit_px if entry_type == "LIMIT" and limit_px else (request.price or 0) or 0)
            if sanity_px > 0:
                blocked, block_reason = _market_sanity_block(
                    self._account,
                    str(request.symbol or "MNQ"),
                    sanity_px,
                )
                if blocked:
                    payload["status"] = "blocked_fill_divergence"
                    payload["block_reason"] = block_reason
                    payload["sanity_price"] = sanity_px
                    self._audit("order_blocked", payload)
                    logger.error("NT8 entry blocked (fill divergence): %s", block_reason)
                    return payload
            ok = _nt8_client_command(
                "PLACE",
                self._account,
                self._instrument,
                nt8_action,
                qty,
                entry_type,
                order_id,
                limit_px if entry_type == "LIMIT" else 0.0,
            )
            payload["status"] = "submitted" if ok else "nt8_rejected"
            payload["order_id"] = order_id
            payload["nt8_action"] = nt8_action
            payload["order_type"] = entry_type
            payload["limit_price"] = limit_px if entry_type == "LIMIT" else None
        elif request.action in (OrderAction.EXIT, OrderAction.FLATTEN):
            close_action = "SELL" if request.side == Side.LONG else "BUYTOCOVER"
            order_id = self._next_order_id(f"EXIT_{tag}")
            ok = _nt8_client_command(
                "PLACE", self._account, self._instrument, close_action, qty, "MARKET", order_id
            )
            payload["status"] = "submitted" if ok else "nt8_rejected"
            payload["order_id"] = order_id
            payload["nt8_action"] = close_action
            if ok:
                verify = self.cancel_orphan_orders(reason=f"post_exit_{tag}")
                payload["orphan_guard"] = {
                    "orphan_remaining": verify.get("orphan_remaining"),
                    "cancelled": len(verify.get("cancelled") or []),
                }
        else:
            payload["status"] = "unsupported_action"
            ok = False

        self._audit("order_submitted" if ok else "order_failed", payload)
        if ok:
            logger.info("NT8 order OK: %s %s qty=%s id=%s", payload.get("nt8_action"), self._instrument, qty, payload.get("order_id"))
        return payload

    def on_websocket_bar(self, bar: dict[str, Any]) -> None:
        self._audit("websocket_bar_stub", {"keys": sorted(bar.keys())})

    def _audit(self, event: str, data: dict[str, Any]) -> None:
        record = {"event": event, **data}
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
