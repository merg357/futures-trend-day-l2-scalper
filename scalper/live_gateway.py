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


def _mes_tick_size() -> float:
    try:
        return float(os.getenv("MES_TICK_SIZE", "0.25") or 0.25)
    except (TypeError, ValueError):
        return 0.25


def _round_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 2)


def _read_exec_market_quote(symbol: str) -> dict[str, float]:
    """Best bid/ask/mid for execution symbol from FuturesBot orderflow."""
    of_path = Path(os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")) / "state" / "orderflow.json"
    out = {"bid": 0.0, "ask": 0.0, "mid": 0.0}
    if not of_path.exists():
        return out
    try:
        row = (json.loads(of_path.read_text(encoding="utf-8")).get(symbol.upper()) or {})
    except Exception:
        return out
    if not isinstance(row, dict):
        return out
    bids = row.get("bid_levels") or []
    asks = row.get("ask_levels") or []
    if bids:
        out["bid"] = float(bids[0].get("price") or 0)
    if asks:
        out["ask"] = float(asks[0].get("price") or 0)
    mid = float(row.get("mid_price") or row.get("mid") or 0)
    if mid <= 0 and out["bid"] > 0 and out["ask"] > 0:
        mid = (out["bid"] + out["ask"]) / 2.0
    elif mid <= 0 and out["bid"] > 0:
        mid = out["bid"]
    elif mid <= 0 and out["ask"] > 0:
        mid = out["ask"]
    out["mid"] = mid
    return out


def _normalize_protective_stop(
    side: Side,
    stop_price: float,
    *,
    symbol: str,
    tick: float | None = None,
    stop_ticks: int = 10,
) -> float:
    """Ensure NT8-acceptable stop: long SELL stop below bid; short BUY stop above ask."""
    tick = tick or _mes_tick_size()
    stop_price = _round_tick(float(stop_price), tick)
    q = _read_exec_market_quote(symbol)
    bid, ask, mid = q["bid"], q["ask"], q["mid"]
    if side == Side.LONG:
        ref = bid if bid > 0 else mid
        if ref > 0 and stop_price >= ref:
            stop_price = _round_tick(ref - tick, tick)
        if mid > 0 and stop_price >= mid:
            stop_price = _round_tick(mid - stop_ticks * tick, tick)
            if bid > 0 and stop_price >= bid:
                stop_price = _round_tick(bid - tick, tick)
    else:
        ref = ask if ask > 0 else mid
        if ref > 0 and stop_price <= ref:
            stop_price = _round_tick(ref + tick, tick)
        if mid > 0 and stop_price <= mid:
            stop_price = _round_tick(mid + stop_ticks * tick, tick)
            if ask > 0 and stop_price <= ask:
                stop_price = _round_tick(ask + tick, tick)
    return stop_price


def _nt8_client_command(
    command: str,
    account: str,
    instrument: str,
    action: str,
    qty: int,
    order_type: str,
    order_id: str,
    limit_price: float = 0.0,
    stop_price: float = 0.0,
    *,
    tif: str = "DAY",
) -> bool:
    ps_cmd = (
        f'$asm = [System.Reflection.Assembly]::LoadFile("{_NT8_DLL}"); '
        f'$c = [Activator]::CreateInstance($asm.GetType("NinjaTrader.Client.Client")); '
        f'$c.SetUp("{_NT8_HOST}", {_NT8_PORT}); '
        f'Start-Sleep -Milliseconds 500; '
        f'$c.ConfirmOrders(0); '
        f'$r = $c.Command("{command}", "{account}", "{instrument}", "{action}", '
        f'{qty}, "{order_type}", {limit_price}, {stop_price}, "{tif}", "", "{order_id}", "", ""); '
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


def _nt8_place_stop_market(
    account: str,
    instrument: str,
    action: str,
    qty: int,
    order_id: str,
    stop_price: float,
) -> tuple[bool, str]:
    """Place protective STOPMARKET; prefer GTC on Sim101, fall back to DAY."""
    for tif in ("GTC", "DAY"):
        if _nt8_client_command(
            "PLACE",
            account,
            instrument,
            action,
            qty,
            "STOPMARKET",
            order_id,
            0.0,
            stop_price,
            tif=tif,
        ):
            return True, tif
    return False, ""


class LiveGateway:
    """Broker adapter — sends NT8 demo orders when NT8_DEMO_ORDERS=true."""

    def __init__(
        self,
        log_dir: str | Path = "data/live",
        *,
        execution_symbol: str | None = None,
        order_prefix: str | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._audit_path = self.log_dir / "gateway_audit.jsonl"
        self._connected = False
        self._order_seq = 0
        self._account = _nt8_account()
        sym = execution_symbol or os.getenv("SCALPER_SYMBOL", "MNQ")
        self._execution_symbol = str(sym).upper()[:3]
        if order_prefix:
            os.environ["NT8_ORDER_PREFIX"] = order_prefix
        self._instrument = _resolve_instrument(sym)

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

    def submit_stop_order(
        self,
        *,
        side: Side,
        quantity: int,
        stop_price: float,
        reason: str = "initial_stop",
        entry_price: float | None = None,
    ) -> dict[str, Any]:
        """Submit protective STOP order after entry fill (MES raw test)."""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "stop",
            "symbol": self._execution_symbol,
            "side": side.value,
            "quantity": quantity,
            "stop_price": stop_price,
            "reason": reason,
            "account": self._account,
            "instrument": self._instrument,
        }
        if not nt8_orders_enabled():
            payload["status"] = "blocked_paper_mode"
            self._audit("stop_blocked", payload)
            return payload
        require_live_trading("submit_stop_order")
        qty = max(1, int(quantity or 1))
        tag = reason.replace(" ", "_")[:20] or "STOP"
        order_id = self._next_order_id(f"STP_{tag}")
        nt8_action = "SELL" if side == Side.LONG else "BUYTOCOVER"
        tick = _mes_tick_size()
        stop_ticks = int(os.getenv("MES_STOP_LOSS_TICKS", "10") or 10)
        quote = _read_exec_market_quote(self._execution_symbol)
        norm_stop = _normalize_protective_stop(
            side,
            float(stop_price),
            symbol=self._execution_symbol,
            tick=tick,
            stop_ticks=stop_ticks,
        )
        payload["stop_price_requested"] = float(stop_price)
        payload["stop_price"] = norm_stop
        payload["market_bid"] = quote["bid"] or None
        payload["market_ask"] = quote["ask"] or None
        payload["market_mid"] = quote["mid"] or None
        if entry_price is not None and entry_price > 0:
            from scalper.exit_rules import stop_is_correct_side_of_entry, stop_side_metadata

            payload.update(stop_side_metadata(side, entry_price, norm_stop, tick))
            if not payload.get("stop_is_correct_side_of_entry"):
                payload["status"] = "blocked_stop_wrong_side"
                self._audit("stop_blocked", payload)
                logger.critical(
                    "Refusing stop on wrong side: entry=%.2f stop=%.2f side=%s",
                    entry_price, norm_stop, side.value,
                )
                return payload
        if side == Side.LONG and quote["bid"] > 0 and norm_stop >= quote["bid"]:
            payload["status"] = "blocked_stop_above_bid"
            self._audit("stop_blocked", payload)
            return payload
        if side == Side.SHORT and quote["ask"] > 0 and norm_stop <= quote["ask"]:
            payload["status"] = "blocked_stop_below_ask"
            self._audit("stop_blocked", payload)
            return payload
        ok, tif = _nt8_place_stop_market(
            self._account,
            self._instrument,
            nt8_action,
            qty,
            order_id,
            norm_stop,
        )
        payload["tif"] = tif or None
        payload["status"] = "submitted" if ok else "nt8_rejected"
        payload["order_id"] = order_id
        payload["nt8_action"] = nt8_action
        if ok:
            try:
                import sys
                import time as _time

                fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
                if fb_root not in sys.path:
                    sys.path.insert(0, fb_root)
                from l2_nt8_orphan_guard import query_account_orders

                _time.sleep(1.5)
                q = query_account_orders(
                    self._account,
                    symbol_instruments=[(self._execution_symbol, self._instrument)],
                )
                statuses = (q.get("order_statuses") or {})
                nt8_status = str(statuses.get(order_id) or "")
                payload["nt8_order_status"] = nt8_status or None
                if nt8_status.lower() in {"rejected", "cancelled"}:
                    payload["status"] = "nt8_rejected"
                    ok = False
            except Exception as exc:
                logger.warning("stop post-submit verify skipped: %s", exc)
        self._audit("stop_submitted" if ok else "stop_failed", payload)
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
            entry_mode = os.getenv("MES_ENTRY_ORDER_MODE", "MARKETABLE_LIMIT").strip().upper()
            chase_ticks = float(os.getenv("MES_ENTRY_CHASE_TICKS", "0") or 0)
            tick = float(os.getenv("MES_TICK_SIZE", "0.25") or 0.25)
            max_spread = float(os.getenv("MES_MAX_SPREAD_TICKS", "2") or 2)
            resample = os.getenv("MES_RESAMPLE_QUOTE", "1").strip().lower() in {"1", "true", "yes"}
            mes_quote = _read_exec_market_quote(self._execution_symbol)
            payload["mes_bid_at_submit"] = mes_quote["bid"] or None
            payload["mes_ask_at_submit"] = mes_quote["ask"] or None
            if mes_quote["bid"] > 0 and mes_quote["ask"] > 0:
                payload["mes_spread_at_submit"] = (mes_quote["ask"] - mes_quote["bid"]) / tick
            payload["entry_order_mode"] = entry_mode
            if resample and mes_quote["bid"] > 0 and mes_quote["ask"] > 0:
                spread_ticks = (mes_quote["ask"] - mes_quote["bid"]) / tick
                if spread_ticks > max_spread:
                    payload["status"] = "blocked_spread"
                    payload["cancel_reason"] = "cancel_spread"
                    self._audit("order_blocked", payload)
                    return payload
            if entry_mode == "MARKET_DIAGNOSTIC":
                entry_type = "MARKET"
                limit_px = 0.0
            elif chase_ticks > 0 and (resample or (request.price and request.price > 0)):
                entry_type = "LIMIT"
                chase = chase_ticks * tick
                if resample and mes_quote["bid"] > 0 and mes_quote["ask"] > 0:
                    limit_px = mes_quote["ask"] + chase if request.side == Side.LONG else mes_quote["bid"] - chase
                elif request.price and request.price > 0:
                    limit_px = float(request.price) + chase if request.side == Side.LONG else float(request.price) - chase
                else:
                    entry_type = "MARKET"
                    limit_px = 0.0
            else:
                try:
                    import sys

                    fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
                    if fb_root not in sys.path:
                        sys.path.insert(0, fb_root)
                    from nt8_market_sanity import demo_entry_order_spec

                    entry_type, limit_px = demo_entry_order_spec(
                        str(request.symbol or self._execution_symbol),
                        float(request.price or 0) or None,
                        side=request.side.value,
                    )
                except Exception:
                    entry_type, limit_px = "MARKET", 0.0
            sanity_px = float(limit_px if entry_type == "LIMIT" and limit_px else (request.price or 0) or 0)
            if sanity_px > 0:
                blocked, block_reason = _market_sanity_block(
                    self._account,
                    str(request.symbol or self._execution_symbol),
                    sanity_px,
                )
                block_on_div = os.getenv("MES_BLOCK_FILL_DIVERGENCE", "0").strip().lower() in {"1", "true", "yes"}
                log_div = os.getenv("MES_LOG_FILL_DIVERGENCE", "1").strip().lower() in {"1", "true", "yes"}
                payload["would_have_blocked_fill_divergence"] = blocked
                if blocked:
                    payload["fill_divergence_reason"] = block_reason
                    if log_div:
                        self._audit("fill_divergence_log", payload)
                    if block_on_div:
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
