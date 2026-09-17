"""
Agent orchestration (PRD section 12/13): wires together
validate_data -> run_forecast -> evaluate_actions -> build_explanation_packet
-> explain_recommendation, mirroring the PRD's tool-contract pipeline.

Scope simplification vs. PRD: the `generate_scenarios` tool (Monte Carlo
demand trajectories) is not implemented in this MVP scaffold. The
decision engine consumes forecast quantiles (P10/P50/P90) directly
instead of sampled trajectories. Add a scenario-generation step here
once the real forecasting model and decision engine need it (PRD 10.3 /
11 mention block-bootstrap / Monte Carlo as the next step).
"""

from datetime import timedelta

from django.utils import timezone

from apps.decisionengine.services import DecisionEngine
from apps.forecasting.services import get_forecast_provider
from apps.skus.models import SKU

from .dataclasses import ExplanationPacket
from .explainer import get_explainer

STALE_DATA_THRESHOLD = timedelta(hours=3)


class Agent:
    def __init__(self, forecast_provider=None, decision_engine=None, explainer=None):
        self.forecast_provider = forecast_provider or get_forecast_provider()
        self.decision_engine = decision_engine or DecisionEngine()
        self.explainer = explainer or get_explainer()

    def validate_data(self, sku: SKU, now) -> list[str]:
        """`validate_data` tool: freshness/missing-data checks (FR-A01)."""
        warnings = []
        latest = sku.observations.order_by("-timestamp").first()
        if latest is None:
            warnings.append("Tidak ada data historis untuk SKU ini.")
            return warnings
        if (now - latest.timestamp) > STALE_DATA_THRESHOLD:
            warnings.append(
                f"Data terakhir {latest.timestamp:%Y-%m-%d %H:%M}, lebih dari "
                f"{STALE_DATA_THRESHOLD.total_seconds() / 3600:.0f} jam yang lalu."
            )
        if latest.stockout_flag:
            warnings.append(
                "Stockout terdeteksi pada observasi terakhir; penjualan historis "
                "mungkin under-count demand asli (censoring)."
            )
        return warnings

    def run(self, sku: SKU, now=None, overrides: dict | None = None) -> dict:
        latest_obs = sku.observations.order_by("-timestamp").first()
        if now is None and latest_obs is not None:
            now = latest_obs.timestamp
        else:
            now = now or timezone.now()
        decision_config = sku.decision_config
        warnings = self.validate_data(sku, now)

        forecasts = self.forecast_provider.run_forecast(sku, cutoff=now)
        forecast_48h = next(f for f in forecasts if f.horizon_hours == 48)

        latest_obs = sku.observations.order_by("-timestamp").first()
        current_stock = latest_obs.stock_on_hand if latest_obs else 0

        decision = self.decision_engine.evaluate_actions(
            sku=sku,
            decision_config=decision_config,
            forecast=forecast_48h,
            current_stock=current_stock,
            now=now,
            overrides=overrides,
        )

        packet = ExplanationPacket(
            sku_id=sku.sku_id,
            forecast=forecast_48h,
            decision=decision,
            warnings=warnings,
        )
        explanation_text = self.explainer.explain(packet)

        return {
            "now": now,
            "warnings": warnings,
            "forecasts": forecasts,
            "forecast_48h": forecast_48h,
            "current_stock": current_stock,
            "decision": decision,
            "packet": packet,
            "explanation_text": explanation_text,
        }
