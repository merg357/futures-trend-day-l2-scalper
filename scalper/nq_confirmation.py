"""NQ confirmation veto — blocks ES entries when NQ flow strongly disagrees."""

from __future__ import annotations

import pandas as pd

from scalper.config import NqConfirmationConfig, ScalperConfig
from scalper.flow_signals import compute_flow_signal, enrich_bar_from_orderflow
from scalper.models import Side


def nq_veto_reason(
    es_side: Side,
    config: ScalperConfig,
    *,
    nq_row: pd.Series | None = None,
) -> str | None:
    """Return veto reason when NQ book strongly opposes the ES entry side."""
    nq_cfg: NqConfirmationConfig = config.nq_confirmation
    if not nq_cfg.use_nq_confirmation:
        return None

    root = config.confirmation_root()
    if not root:
        return None

    row = nq_row
    if row is None:
        enriched = enrich_bar_from_orderflow(
            root,
            pd.Series({"close": 0.0}),
            max_age_sec=config.entry.orderflow_max_age_sec,
        )
        if enriched is None:
            return "nq_book_stale"
        row = enriched

    flow = compute_flow_signal(row, None, config.flow)
    threshold = float(nq_cfg.nq_veto_threshold or 0)

    if es_side == Side.LONG:
        if flow.side == Side.SHORT and flow.score >= threshold:
            return f"nq_veto_bearish score={flow.score:.0f}"
        imb = float(row.get("imbalance") or 0)
        if imb <= -config.flow.imbalance_threshold and flow.score >= threshold * 0.8:
            return f"nq_veto_imbalance_bearish imb={imb:.2f}"
    elif es_side == Side.SHORT:
        if flow.side == Side.LONG and flow.score >= threshold:
            return f"nq_veto_bullish score={flow.score:.0f}"
        imb = float(row.get("imbalance") or 0)
        if imb >= config.flow.imbalance_threshold and flow.score >= threshold * 0.8:
            return f"nq_veto_imbalance_bullish imb={imb:.2f}"

    return None
