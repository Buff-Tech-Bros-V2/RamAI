"""
Lightweight API / HTMX-partial views for the RamAI dashboard.

These views re-use the same Agent pipeline as the main index view
(apps.dashboard.views.index). They exist only to serve:

1. JSON data for Chart.js (chart-data endpoint)
2. HTMX partial HTML swaps (dashboard-content, whatif)

No new business logic is introduced here — everything delegates to
Agent().run() which orchestrates forecast → decision → explanation.
"""

import json
import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from apps.agent.orchestrator import Agent
from apps.skus.models import SKU

logger = logging.getLogger(__name__)


def _parse_overrides(request) -> dict:
    """Extract what-if overrides from GET/POST params (shared helper)."""
    overrides = {}
    source = request.POST if request.method == "POST" else request.GET

    capacity = source.get("capacity_minutes")
    if capacity:
        try:
            overrides["daily_capacity_minutes"] = float(capacity)
        except ValueError:
            pass

    capital = source.get("capital")
    if capital:
        try:
            overrides["working_capital_limit"] = float(capital)
        except ValueError:
            pass

    window = source.get("window_hours")
    if window:
        try:
            overrides["decision_window_hours"] = max(1.0, float(window))
        except ValueError:
            pass

    return overrides


def chart_data(request):
    """Return JSON for Chart.js charts."""
    sku_id = request.GET.get("sku")
    if not sku_id:
        return JsonResponse({"error": "sku parameter required"}, status=400)

    sku = get_object_or_404(SKU, pk=sku_id)
    overrides = _parse_overrides(request)
    result = Agent().run(sku, overrides=overrides)

    # Recent 24h observations for demand chart
    recent = list(sku.observations.order_by("-timestamp")[:24])
    recent.reverse()

    demand_data = {
        "timestamps": [obs.timestamp.strftime("%d/%m %H:%M") for obs in recent],
        "orders": [obs.orders_created for obs in recent],
        "stock": [obs.stock_on_hand for obs in recent],
    }

    # Forecast quantile comparison
    forecast_data = {
        "labels": [f"{f.horizon_hours}h" for f in result["forecasts"]],
        "p10": [f.p10 for f in result["forecasts"]],
        "p50": [f.p50 for f in result["forecasts"]],
        "p90": [f.p90 for f in result["forecasts"]],
    }

    return JsonResponse({
        "demand": demand_data,
        "forecast": forecast_data,
    })


def dashboard_partial(request):
    """Return the main dashboard content as an HTMX partial (no base layout)."""
    sku_id = request.GET.get("sku")
    if not sku_id:
        return render(request, "dashboard/_partials/empty_state.html")

    sku = get_object_or_404(SKU, pk=sku_id)
    overrides = _parse_overrides(request)
    result = Agent().run(sku, overrides=overrides)

    recent_history = list(sku.observations.order_by("-timestamp")[:24])
    recent_history.reverse()

    context = {
        "sku": sku,
        "selected_sku_id": sku_id,
        "decision_config": sku.decision_config,
        "result": result,
        "recent_history": recent_history,
        "whatif": overrides,
        "drafts": sku.action_plan_drafts.all()[:5],
        "chart_data_json": _build_chart_json(recent_history, result),
    }
    return render(request, "dashboard/_partials/dashboard_content.html", context)


def whatif_recalculate(request):
    """HTMX endpoint: recalculate with what-if overrides, return partial."""
    sku_id = request.POST.get("sku") or request.GET.get("sku")
    if not sku_id:
        return JsonResponse({"error": "sku parameter required"}, status=400)

    sku = get_object_or_404(SKU, pk=sku_id)
    overrides = _parse_overrides(request)
    result = Agent().run(sku, overrides=overrides)

    recent_history = list(sku.observations.order_by("-timestamp")[:24])
    recent_history.reverse()

    context = {
        "sku": sku,
        "selected_sku_id": sku_id,
        "decision_config": sku.decision_config,
        "result": result,
        "recent_history": recent_history,
        "whatif": overrides,
        "drafts": sku.action_plan_drafts.all()[:5],
        "chart_data_json": _build_chart_json(recent_history, result),
    }
    return render(request, "dashboard/_partials/dashboard_content.html", context)


@require_POST
def explanation_partial(request, sku_id):
    """HTMX endpoint: run the LLM explanation for one SKU and swap it in.

    This is the only view that asks `Agent.run` to explain, so a provider
    call happens when the user presses "Analisis" -- not on every page load.
    POST-only so a browser prefetch or a refresh cannot spend the quota.
    """
    sku = get_object_or_404(SKU, pk=sku_id)
    overrides = _parse_overrides(request)

    context = {
        "sku": sku,
        "decision_config": sku.decision_config,
        "whatif": overrides,
    }
    try:
        result = Agent().run(sku, overrides=overrides, explain=True)
    except Exception:
        # The explainer already falls back to its offline template on a failed
        # provider call, so reaching here means the pipeline itself broke.
        # Keep the panel usable: show the reason and let the user retry.
        logger.exception("Explanation failed for %s", sku_id)
        context["explanation_error"] = (
            "Penjelasan gagal dibuat. Angka rekomendasi tetap valid — coba lagi sebentar."
        )
    else:
        context["explanation_text"] = result["explanation_text"]

    return render(request, "dashboard/_partials/ai_explanation.html", context)


def _build_chart_json(recent_history, result) -> str:
    """Build JSON string for inline chart data in templates."""
    demand = {
        "timestamps": [obs.timestamp.strftime("%d/%m %H:%M") for obs in recent_history],
        "orders": [obs.orders_created for obs in recent_history],
        "stock": [obs.stock_on_hand for obs in recent_history],
    }
    forecast = {
        "labels": [f"{f.horizon_hours} jam" for f in result["forecasts"]],
        "p10": [f.p10 for f in result["forecasts"]],
        "p50": [f.p50 for f in result["forecasts"]],
        "p90": [f.p90 for f in result["forecasts"]],
    }
    return json.dumps({"demand": demand, "forecast": forecast})
