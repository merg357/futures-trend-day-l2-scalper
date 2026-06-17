"""MES execution / ES signal / NQ confirmation runner (raw test mode).

Routes orders ONLY to MES via NT8. ES orderflow drives flow_burst entries;
NQ book vetoes only. Sim/testing — do not enable live trading without review.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from scalper.config import load_config
from scalper.live_gateway import LiveGateway
from scalper.paper_runner import (
    _acquire_single_instance,
    _bar_csv_missing_message,
    _enforce_runner_safety,
    resolve_bar_csv_path,
    run_follow,
    run_replay,
)
from scalper.trading_safety import demo_nt8_orders_enabled, paper_only_mode

logger = logging.getLogger(__name__)

_ENT_ORDER_MARKERS = ("_ENT_", "_ENT")


def _ent_order_prefix(config) -> str:
    raw = str(config.mes_execution.order_id_prefix or "L2MES").strip()
    tag = raw if raw.endswith("_") else f"{raw}_"
    return f"{tag}ENT"


def _has_working_ent_order(gateway: LiveGateway, config) -> tuple[bool, str]:
    """True when NT8 still has a working L2MES_ENT (or configured prefix) entry."""
    prefix = _ent_order_prefix(config)
    try:
        import sys

        fb_root = os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")
        if fb_root not in sys.path:
            sys.path.insert(0, fb_root)
        from l2_nt8_orphan_guard import query_account_orders

        q = query_account_orders(
            gateway._account,
            symbol_instruments=[(gateway._execution_symbol, gateway._instrument)],
        )
        if not q.get("api_ok"):
            return False, ""
        for order in q.get("working_orders") or []:
            oid = str(order.get("order_id") or "")
            upper = oid.upper()
            if upper.startswith(prefix.upper()) or any(m in upper for m in _ENT_ORDER_MARKERS):
                return True, oid
    except Exception as exc:
        logger.debug("ENT working-order query skipped: %s", exc)
    return False, ""


def mes_entry_blocked_reason(
    gateway: LiveGateway | None,
    config,
    *,
    pending_entry: object | None = None,
) -> str | None:
    """Block duplicate MES entries when NT8 is non-flat or ENT is still working."""
    if gateway is None or not config.is_mes_es_nq_mode():
        return None
    if pending_entry is not None:
        return "pending_l2mes_ent_in_memory"
    pos = gateway.query_market_position()
    if pos is not None and pos != 0:
        return f"mes_position_nonzero={pos}"
    working, oid = _has_working_ent_order(gateway, config)
    if working:
        return f"pending_l2mes_ent_working={oid}"
    return None

DEFAULT_CONFIG = "configs/production/mes_es_nq_raw_test.yaml"
STATE_STATUS = Path(os.getenv("FUTURESBOT_ROOT", r"C:\FuturesBot")) / "state" / "mes_es_nq_runner_status.json"


def _apply_mes_env(config) -> None:
    """Set NT8/gateway env for MES execution without touching global git config."""
    os.environ.setdefault("SCALPER_SYMBOL", config.execution_root())
    os.environ.setdefault("NT8_INSTRUMENT", os.getenv("NT8_MES_INSTRUMENT", ""))
    os.environ["NT8_ORDER_PREFIX"] = config.mes_execution.order_id_prefix
    os.environ["MES_ENTRY_CHASE_TICKS"] = str(config.mes_execution.entry_chase_ticks)
    os.environ["MES_TICK_SIZE"] = str(config.tick_size)
    os.environ["MES_ENTRY_TIMEOUT_MS"] = str(config.mes_execution.entry_timeout_ms)
    os.environ["MES_ENTRY_ORDER_MODE"] = config.mes_execution.entry_order_mode
    os.environ["MES_MAX_SPREAD_TICKS"] = str(config.mes_execution.max_spread_ticks)
    os.environ["MES_RESAMPLE_QUOTE"] = "1" if config.mes_execution.resample_mes_quote_before_submit else "0"
    os.environ["MES_BLOCK_FILL_DIVERGENCE"] = "1" if config.mes_execution.block_on_fill_divergence else "0"
    os.environ["MES_LOG_FILL_DIVERGENCE"] = "1" if config.mes_execution.log_fill_divergence else "0"
    os.environ["MES_STOP_LOSS_TICKS"] = str(config.exit.stop_loss_ticks)


def _write_runner_status(config, *, log_dir: Path, data_path: Path, mode: str) -> None:
    payload = {
        "strategy_mode": config.mode,
        "runner": "mes_es_nq_runner",
        "execution_instrument": config.execution_root(),
        "signal_instrument": config.signal_root(),
        "confirmation_instrument": config.confirmation_root(),
        "config_path": os.getenv("SCALPER_CONFIG", DEFAULT_CONFIG),
        "bar_csv_path": str(data_path),
        "log_dir": str(log_dir),
        "runner_mode": mode,
        "paper_only": paper_only_mode(),
        "nt8_demo_orders": demo_nt8_orders_enabled(),
        "order_id_prefix": config.mes_execution.order_id_prefix,
    }
    try:
        STATE_STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATE_STATUS.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("status write failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MES execution with ES signal and NQ confirmation (raw test)"
    )
    parser.add_argument("--config", default=os.getenv("SCALPER_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--data", default="", help="ES/MNQ bar CSV for trend context")
    parser.add_argument("--mode", choices=["replay", "follow"], default=os.getenv("RUNNER_MODE", "follow"))
    parser.add_argument("--log-dir", default=os.getenv("LIVE_LOG_DIR", "data/live_mes_es_nq"))
    parser.add_argument("--poll-seconds", type=float, default=float(
        os.getenv("POLL_SECONDS", os.getenv("MES_DECISION_LOOP_SEC", "0.25"))
    ))
    args = parser.parse_args()

    config = load_config(args.config)
    if not config.is_mes_es_nq_mode():
        raise SystemExit(
            f"Config mode must be MES_ES_NQ_RAW_TEST (got {config.mode!r}). "
            f"Use --config {DEFAULT_CONFIG}"
        )

    _enforce_runner_safety()
    _acquire_single_instance()
    _apply_mes_env(config)

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    gateway = LiveGateway(
        log_dir=log_dir,
        execution_symbol=config.execution_root(),
        order_prefix=config.mes_execution.order_id_prefix,
    )
    gateway.connect()
    gateway.cancel_orphan_orders(reason="mes_es_nq_startup")

    data_path = resolve_bar_csv_path(args.data)
    if args.mode == "replay" and not args.data.strip():
        raise SystemExit(_bar_csv_missing_message())

    _write_runner_status(config, log_dir=log_dir, data_path=data_path, mode=args.mode)

    logger.info(
        "MES/ES/NQ raw test: exec=%s signal=%s confirm=%s orders=%s only",
        config.execution_root(),
        config.signal_root(),
        config.confirmation_root(),
        config.execution_root(),
    )

    if args.mode == "replay":
        summary = run_replay(config, data_path, log_dir=log_dir, gateway=gateway)
        print(json.dumps(summary, indent=2))
    else:
        run_follow(
            config,
            data_path,
            log_dir=log_dir,
            poll_seconds=args.poll_seconds,
            gateway=gateway,
        )


if __name__ == "__main__":
    main()
