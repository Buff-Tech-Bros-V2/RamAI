import dataclasses
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.orchestrator import Agent
from apps.decisionengine.models import ActionPlanDraft
from apps.skus.models import SKU

DEADLINE_SOON_HOURS = 6
SURGE_PERSISTENCE_THRESHOLD = 0.6

STATUS_AMAN = "AMAN"
STATUS_PERLU_PERHATIAN = "PERLU_PERHATIAN"
STATUS_DEADLINE_DEKAT = "DEADLINE_DEKAT"


def summary(request):
    skus = SKU.objects.select_related("decision_config").all()

    if not skus.exists():
        return render(request, "dashboard/summary.html", {"no_data": True})

    rows = []
    for sku in skus:
        result = Agent().run(sku)
        decision = result["decision"]
        now = result["now"]
        deadline = sku.decision_config.commitment_deadline
        hours_to_deadline = (deadline - now).total_seconds() / 3600

        if hours_to_deadline <= DEADLINE_SOON_HOURS:
            status = STATUS_DEADLINE_DEKAT
        elif (
            result["warnings"]
            or decision.confidence in ("LOW", "MEDIUM")
            or decision.surge_persistence_48h >= SURGE_PERSISTENCE_THRESHOLD
        ):
            status = STATUS_PERLU_PERHATIAN
        else:
            status = STATUS_AMAN

        rows.append(
            {
                "sku": sku,
                "status": status,
                "hours_to_deadline": hours_to_deadline,
                "confidence": decision.confidence,
                "surge_persistence_48h": decision.surge_persistence_48h,
                "recommended_action": decision.recommended.action,
                "current_stock": result["current_stock"],
            }
        )

    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    status_filter = request.GET.get("status", "")
    confidence_filter = request.GET.get("confidence", "")

    if q:
        q_lower = q.lower()
        rows = [
            r
            for r in rows
            if q_lower in r["sku"].sku_id.lower() or q_lower in r["sku"].name.lower()
        ]
    if category:
        rows = [r for r in rows if r["sku"].product_category == category]
    if status_filter:
        rows = [r for r in rows if r["status"] == status_filter]
    if confidence_filter:
        rows = [r for r in rows if r["confidence"] == confidence_filter]

    alerts = [r for r in rows if r["status"] != STATUS_AMAN]
    categories = sorted(set(skus.values_list("product_category", flat=True)))

    context = {
        "rows": rows,
        "alerts": alerts,
        "categories": categories,
        "q": q,
        "category": category,
        "status_filter": status_filter,
        "confidence_filter": confidence_filter,
    }
    return render(request, "dashboard/summary.html", context)


def sku_detail(request, sku_id):
    sku = get_object_or_404(SKU, pk=sku_id)
    overrides = _parse_overrides(request)

    result = Agent().run(sku, overrides=overrides)

    recent_history = list(sku.observations.order_by("-timestamp")[:24])
    recent_history.reverse()

    # Build inline chart data for Chart.js (no extra API call needed)
    chart_data_json = _build_chart_json(recent_history, result)

    context = {
        "sku": sku,
        "selected_sku_id": sku_id,
        "skus": SKU.objects.select_related("decision_config").all(),
        "decision_config": sku.decision_config,
        "result": result,
        "recent_history": recent_history,
        "whatif": overrides,
        "drafts": sku.action_plan_drafts.all()[:5],
        "chart_data_json": chart_data_json,
    }
    return render(request, "dashboard/sku_detail.html", context)


def sku_history(request, sku_id):
    sku = get_object_or_404(SKU, pk=sku_id)
    drafts = sku.action_plan_drafts.select_related("approved_by").all()
    context = {"sku": sku, "drafts": drafts}
    return render(request, "dashboard/sku_history.html", context)


def _build_chart_json(recent_history, result) -> str:
    """Build JSON string for inline Chart.js data."""
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


def _parse_overrides(request) -> dict:
    overrides = {}

    capacity = request.GET.get("capacity_minutes")
    if capacity:
        try:
            overrides["daily_capacity_minutes"] = float(capacity)
        except ValueError:
            pass

    capital = request.GET.get("capital")
    if capital:
        try:
            overrides["working_capital_limit"] = float(capital)
        except ValueError:
            pass

    deadline = request.GET.get("deadline")
    if deadline:
        parsed = parse_datetime(deadline)
        if parsed:
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            overrides["commitment_deadline"] = parsed

    return overrides


@login_required
def approve_plan(request, sku_id):
    if request.method != "POST":
        return redirect(f"{reverse('dashboard:sku_detail', args=[sku_id])}")

    sku = get_object_or_404(SKU, pk=sku_id)
    result = Agent().run(sku)
    decision = result["decision"]
    rec = decision.recommended

    decision_snapshot = {
        "decision": dataclasses.asdict(decision),
        "forecasts": [dataclasses.asdict(f) for f in result["forecasts"]],
        "confidence": decision.confidence,
        "surge_persistence_48h": decision.surge_persistence_48h,
        "warnings": result["warnings"],
        "current_stock": result["current_stock"],
    }

    ActionPlanDraft.objects.create(
        sku=sku,
        approved_by=request.user,
        recommended_action=rec.action,
        commit_now_units=rec.commit_now_units,
        commit_later_units=rec.commit_later_units,
        required_capital=rec.required_capital,
        expected_contribution=rec.expected_contribution,
        decision_snapshot=decision_snapshot,
    )
    messages.success(
        request,
        f"Draf rencana untuk {sku.sku_id} tersimpan (belum dieksekusi, "
        "menunggu tindak lanjut manual). MVP tidak melakukan pembelian otomatis.",
    )
    return redirect(f"{reverse('dashboard:sku_detail', args=[sku_id])}")
