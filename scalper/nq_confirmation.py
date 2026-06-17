"""NQ confirmation veto — blocks ES entries when NQ flow strongly disagrees."""

from __future__ import annotations

import pandas as pd

from scalper.config import NqConfirmationConfig, ScalperConfig
from scalper.flow_signals import compute_flow_signal, enrich_bar_from_orderflow
from scalper.models import Side


def _strict_veto_reason(
    es_side: Side,
    flow,
    row: pd.Series,
    nq_cfg: NqConfirmationConfig,
    flow_cfg,
) -> str | None:
    """Legacy strict rule: flow >= 55 or OBI beyond +/-0.55 with flow >= 44."""
    threshold = float(nq_cfg.nq_veto_threshold or 55)
    obi_thresh = float(flow_cfg.imbalance_threshold or 0.55)
    obi_min_flow = threshold * 0.8
    if es_side == Side.LONG:
        if flow.side == Side.SHORT and flow.score >= threshold:
            return f"nq_veto_strict_bearish score={flow.score:.0f}"
        imb = float(row.get("imbalance") or 0)
        if imb <= -obi_thresh and flow.score >= obi_min_flow:
            return f"nq_veto_strict_imbalance_bearish imb={imb:.2f}"
    elif es_side == Side.SHORT:
        if flow.side == Side.LONG and flow.score >= threshold:
            return f"nq_veto_strict_bullish score={flow.score:.0f}"
        imb = float(row.get("imbalance") or 0)
        if imb >= obi_thresh and flow.score >= obi_min_flow:
            return f"nq_veto_strict_imbalance_bullish imb={imb:.2f}"
    return None


def _soft_veto_reason(
    es_side: Side,
    flow,
    row: pd.Series,
    nq_cfg: NqConfirmationConfig,
) -> str | None:
    """Soft raw-test rule: opposing flow >= 65 or OBI beyond +/-0.65 with flow >= 55."""
    flow_thresh = float(nq_cfg.nq_opposing_flow_veto or 65)
    obi_thresh = float(nq_cfg.nq_obi_veto_threshold or 0.65)
    obi_min_flow = float(nq_cfg.nq_veto_min_flow_for_obi or 55)
    if es_side == Side.LONG:
        if flow.side == Side.SHORT and flow.score >= flow_thresh:
            return f"nq_veto_bearish score={flow.score:.0f}"
        imb = float(row.get("imbalance") or 0)
        if imb <= -obi_thresh and flow.score >= obi_min_flow:
            return f"nq_veto_imbalance_bearish imb={imb:.2f}"
    elif es_side == Side.SHORT:
        if flow.side == Side.LONG and flow.score >= flow_thresh:
            return f"nq_veto_bullish score={flow.score:.0f}"
        imb = float(row.get("imbalance") or 0)
        if imb >= obi_thresh and flow.score >= obi_min_flow:
            return f"nq_veto_imbalance_bullish imb={imb:.2f}"
    return None


def nq_veto_comparison(
    es_side: Side,
    config: ScalperConfig,
    *,
    nq_row: pd.Series | None = None,
) -> dict[str, object]:
    """Return strict vs soft NQ veto comparison for logging/dashboard."""
    nq_cfg = config.nq_confirmation
    root = config.confirmation_root()
    out: dict[str, object] = {
        "nq_veto_strict_would_block": False,
        "nq_veto_soft_blocks": False,
        "nq_veto_reason": None,
        "nq_flow_score": None,
        "nq_obi": None,
    }
    if not nq_cfg.use_nq_confirmation or not root:
        return out

    row = nq_row
    if row is None:
        enriched = enrich_bar_from_orderflow(
            root,
            pd.Series({"close": 0.0}),
            max_age_sec=config.entry.orderflow_max_age_sec,
        )
        if enriched is None:
            out["nq_veto_reason"] = "nq_book_stale"
            return out
        row = enriched

    flow = compute_flow_signal(row, None, config.flow)
    out["nq_flow_score"] = flow.score
    out["nq_obi"] = float(row.get("imbalance") or 0)

    strict = _strict_veto_reason(es_side, flow, row, nq_cfg, config.flow)
    soft = _soft_veto_reason(es_side, flow, row, nq_cfg)
    out["nq_veto_strict_would_block"] = strict is not None
    out["nq_veto_soft_blocks"] = soft is not None
    out["nq_veto_reason"] = soft
    return out


def nq_veto_reason(
    es_side: Side,
    config: ScalperConfig,
    *,
    nq_row: pd.Series | None = None,
) -> str | None:
    """Return veto reason when NQ book strongly opposes the ES entry side (soft rule)."""
    return nq_veto_comparison(es_side, config, nq_row=nq_row).get("nq_veto_reason")  # type: ignore[return-value]
