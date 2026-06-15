"""Live broker gateway stub — orders are blocked unless safety gates pass."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from scalper.models import Side
from scalper.trading_safety import live_trading_enabled, require_live_trading

logger = logging.getLogger(__name__)


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


class LiveGateway:
    """Broker adapter stub. Never sends orders unless LIVE_TRADING safety gates pass."""

    def __init__(self, log_dir: str | Path = "data/live") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._audit_path = self.log_dir / "gateway_audit.jsonl"
        self._connected = False

    def connect(self) -> bool:
        """Connect to broker feed. Stub — no network I/O in default build."""
        self._connected = True
        self._audit("connect", {"status": "stub_connected", "live_enabled": live_trading_enabled()})
        logger.info("LiveGateway stub connected (no broker socket in default build).")
        return True

    def disconnect(self) -> None:
        self._connected = False
        self._audit("disconnect", {})

    @property
    def connected(self) -> bool:
        return self._connected

    def submit_order(self, request: OrderRequest) -> dict[str, Any]:
        """Submit order to broker. Blocked unless LIVE_TRADING + confirmation phrase."""
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": request.action.value,
            "symbol": request.symbol,
            "side": request.side.value,
            "quantity": request.quantity,
            "price": request.price,
            "reason": request.reason,
            "live_enabled": live_trading_enabled(),
        }
        if not live_trading_enabled():
            payload["status"] = "blocked_paper_mode"
            self._audit("order_blocked", payload)
            logger.warning("Order blocked (paper mode): %s", request.reason)
            return payload

        require_live_trading("submit_order")
        payload["status"] = "not_implemented"
        self._audit("order_not_implemented", payload)
        logger.error(
            "Live trading gates passed but broker adapter is not implemented. "
            "Wire Rithmic/NinjaTrader API here after validation."
        )
        return payload

    def on_websocket_bar(self, bar: dict[str, Any]) -> None:
        """Optional real-time bar hook. Stub for future NinjaTrader/Rithmic bridge."""
        self._audit("websocket_bar_stub", {"keys": sorted(bar.keys())})

    def _audit(self, event: str, data: dict[str, Any]) -> None:
        record = {"event": event, **data}
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
