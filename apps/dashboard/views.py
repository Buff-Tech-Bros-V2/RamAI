from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.orchestrator import Agent
from apps.decisionengine.models import ActionPlanDraft
from apps.skus.models import SKU


def index(request):
    skus = SKU.objects.select_related("decision_config").all()
    selected_sku_id = request.GET.get("sku") or (
        skus.first().sku_id if skus.exists() else None
    )

    context = {"skus": skus, "selected_sku_id": selected_sku_id}

    if not selected_sku_id:
        context["no_data"] = True
        return render(request, "dashboard/index.html", context)

    sku = get_object_or_404(SKU, pk=selected_sku_id)
    overrides = _parse_overrides(request)

    result = Agent().run(sku, overrides=overrides)

    recent_history = list(sku.observations.order_by("-timestamp")[:24])
    recent_history.reverse()

    context.update(
        {
            "sku": sku,
            "decision_config": sku.decision_config,
            "result": result,
            "recent_history": recent_history,
            "whatif": overrides,
            "drafts": sku.action_plan_drafts.all()[:5],
        }
    )
    return render(request, "dashboard/index.html", context)


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


def approve_plan(request, sku_id):
    if request.method != "POST":
        return redirect(f"{reverse('dashboard:index')}?sku={sku_id}")

    sku = get_object_or_404(SKU, pk=sku_id)
    result = Agent().run(sku)
    rec = result["decision"].recommended

    ActionPlanDraft.objects.create(
        sku=sku,
        recommended_action=rec.action,
        commit_now_units=rec.commit_now_units,
        commit_later_units=rec.commit_later_units,
        required_capital=rec.required_capital,
        expected_contribution=rec.expected_contribution,
    )
    messages.success(
        request,
        f"Draf rencana untuk {sku.sku_id} tersimpan (belum dieksekusi, "
        "menunggu tindak lanjut manual). MVP tidak melakukan pembelian otomatis.",
    )
    return redirect(f"{reverse('dashboard:index')}?sku={sku_id}")
