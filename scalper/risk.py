"""Risk management for backtesting."""

from __future__ import annotations

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
        if self.trades_today >= self.config.risk.max_trades_per_session:
            return False
        if self.daily_pnl <= -self.config.risk.max_daily_loss_dollars:
            self.halted = True
            self.halt_reason = "max_daily_loss"
            return False
        if self.consecutive_losses >= self.config.risk.max_consecutive_losses:
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
        if self.consecutive_losses >= self.config.risk.max_consecutive_losses:
            self.halted = True
            self.halt_reason = "max_consecutive_losses"
        if self.daily_pnl <= -self.config.risk.max_daily_loss_dollars:
            self.halted = True
            self.halt_reason = "max_daily_loss"
