"""Risk management for backtesting."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from scalper.config import ScalperConfig
from scalper.models import Trade


class RiskManager:
    """Session-level risk controls."""

    def __init__(self, config: ScalperConfig) -> None:
        self.config = config
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.halted = False
        self.halt_reason = ""

    def reset_session(self) -> None:
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.halted = False
        self.halt_reason = ""

    def can_enter(self) -> bool:
        if self.halted:
            return False
        flags = self.config.filters
        max_trades = self.config.risk.max_trades_per_session
        if flags.use_trade_cap and max_trades > 0 and self.trades_today >= max_trades:
            return False
        max_loss = self.config.risk.max_daily_loss_dollars
        if flags.use_daily_loss_limit and max_loss > 0 and self.daily_pnl <= -max_loss:
            self.halted = True
            self.halt_reason = "max_daily_loss"
            return False
        max_cl = self.config.risk.max_consecutive_losses
        if max_cl > 0 and self.consecutive_losses >= max_cl:
            self.halted = True
            self.halt_reason = "max_consecutive_losses"
            return False
        return True

    def position_size(self) -> int:
        stop_ticks = self.config.exit.stop_loss_ticks
        risk_per_tick = self.config.tick_value
        if stop_ticks <= 0:
            return self.config.risk.max_contracts
        max_by_risk = int(self.config.risk.risk_per_trade_dollars / (stop_ticks * risk_per_tick))
        return max(1, min(self.config.risk.max_contracts, max_by_risk))

    def record_trade(self, trade: Trade) -> None:
        self.trades_today += 1
        self.daily_pnl += trade.pnl
        if trade.pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        max_cl = self.config.risk.max_consecutive_losses
        if max_cl > 0 and self.consecutive_losses >= max_cl:
            self.halted = True
            self.halt_reason = "max_consecutive_losses"
        max_loss = self.config.risk.max_daily_loss_dollars
        if max_loss > 0 and self.daily_pnl <= -max_loss:
            self.halted = True
            self.halt_reason = "max_daily_loss"

    def hydrate_from_jsonl(
        self,
        trades_path: Path,
        *,
        session_date: date | None = None,
        mode: str = "follow",
    ) -> None:
        """Restore session counters from deduped trades log (follow-mode restart)."""
        if not trades_path.exists():
            return
        seen: set[tuple[str, str, str]] = set()
        records: list[dict[str, Any]] = []
        for line in trades_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if mode and str(rec.get("mode", "")) != mode:
                continue
            exit_time = str(rec.get("exit_time", ""))
            if session_date is not None and exit_time:
                try:
                    if pd.to_datetime(exit_time).date() != session_date:
                        continue
                except (TypeError, ValueError):
                    continue
            key = (
                str(rec.get("entry_time", "")),
                exit_time,
                str(rec.get("side", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(rec)
        records.sort(key=lambda r: (str(r.get("exit_time", "")), str(r.get("entry_time", ""))))
        self.reset_session()
        for rec in records:
            pnl = float(rec.get("pnl", 0.0))
            self.trades_today += 1
            self.daily_pnl += pnl
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0
            max_cl = self.config.risk.max_consecutive_losses
            if max_cl > 0 and self.consecutive_losses >= max_cl:
                self.halted = True
                self.halt_reason = "max_consecutive_losses"
            max_loss = self.config.risk.max_daily_loss_dollars
            if max_loss > 0 and self.daily_pnl <= -max_loss:
                self.halted = True
                self.halt_reason = "max_daily_loss"
                break
