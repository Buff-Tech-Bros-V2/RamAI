import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.orchestrator import Agent
from apps.decisionengine.models import ActionPlanDraft
from apps.skus.models import SKU

from .forms import DecisionConfigForm, SKUForm


def overview(request):
    skus = list(SKU.objects.select_related("decision_config").all())
    product_summaries = []
    total_stock = 0
    attention_count = 0

    for sku in skus:
        latest = sku.observations.order_by("-timestamp").first()
        stock = latest.stock_on_hand if latest else 0
        total_stock += stock
        try:
            result = Agent().run(sku)
            decision = result["decision"]
            persistence = decision.surge_persistence_48h
            recommended = decision.recommended
            needs_attention = persistence >= 0.35 or bool(result["warnings"])
        except (ValueError, AttributeError):
            decision = None
            persistence = 0
            recommended = None
            needs_attention = True
        attention_count += int(needs_attention)
        product_summaries.append(
            {
                "sku": sku,
                "stock": stock,
                "persistence": persistence,
                "recommended": recommended,
                "needs_attention": needs_attention,
                "has_history": latest is not None,
            }
        )

    context = {
        "active_nav": "overview",
        "product_summaries": product_summaries,
        "product_count": len(skus),
        "attention_count": attention_count,
        "total_stock": total_stock,
        "draft_count": ActionPlanDraft.objects.count(),
        "recent_drafts": ActionPlanDraft.objects.select_related("sku")[:5],
    }
    return render(request, "dashboard/overview.html", context)


def decision_center(request, sku_id=None):
    skus = SKU.objects.select_related("decision_config").all()
    selected_sku_id = sku_id or request.GET.get("sku") or (
        skus.first().sku_id if skus.exists() else None
    )

    context = {
        "active_nav": "decisions",
        "skus": skus,
        "selected_sku_id": selected_sku_id,
    }

    if not selected_sku_id:
        context["no_data"] = True
        return render(request, "dashboard/decision_center.html", context)

    sku = get_object_or_404(SKU, pk=selected_sku_id)
    overrides = _parse_overrides(request)

    result = Agent().run(sku, overrides=overrides)

    recent_history = list(sku.observations.order_by("-timestamp")[:24])
    recent_history.reverse()

    # Build inline chart data for Chart.js (no extra API call needed)
    chart_data_json = _build_chart_json(recent_history, result)

    context.update(
        {
            "sku": sku,
            "decision_config": sku.decision_config,
            "result": result,
            "recent_history": recent_history,
            "whatif": overrides,
            "drafts": sku.action_plan_drafts.all()[:5],
            "chart_data_json": chart_data_json,
        }
    )
    return render(request, "dashboard/decision_center.html", context)


def product_list(request):
    products = SKU.objects.select_related("decision_config").prefetch_related(
        "observations"
    )
    rows = []
    for product in products:
        latest = product.observations.order_by("-timestamp").first()
        rows.append({"product": product, "latest": latest})
    return render(
        request,
        "dashboard/products/list.html",
        {"active_nav": "products", "product_rows": rows},
    )


def product_detail(request, sku_id):
    product = get_object_or_404(SKU.objects.select_related("decision_config"), pk=sku_id)
    latest = product.observations.order_by("-timestamp").first()
    recent_drafts = product.action_plan_drafts.all()[:5]
    return render(
        request,
        "dashboard/products/detail.html",
        {
            "active_nav": "products",
            "product": product,
            "latest": latest,
            "recent_drafts": recent_drafts,
        },
    )


@login_required
def product_create(request):
    sku_form = SKUForm(request.POST or None)
    config_form = DecisionConfigForm(request.POST or None)
    if request.method == "POST" and sku_form.is_valid() and config_form.is_valid():
        with transaction.atomic():
            product = sku_form.save()
            config = config_form.save(commit=False)
            config.sku = product
            config.save()
        messages.success(request, f"Produk {product.name} berhasil ditambahkan.")
        return redirect("dashboard:product_detail", sku_id=product.sku_id)
    return render(
        request,
        "dashboard/products/form.html",
        {
            "active_nav": "products",
            "sku_form": sku_form,
            "config_form": config_form,
            "form_title": "Tambah produk",
            "form_intro": "Lengkapi data produk dan batas operasional agar RamAI bisa menghitung rekomendasi.",
        },
    )


@login_required
def product_edit(request, sku_id):
    product = get_object_or_404(SKU.objects.select_related("decision_config"), pk=sku_id)
    sku_form = SKUForm(request.POST or None, instance=product)
    sku_form.fields["sku_id"].disabled = True
    config_form = DecisionConfigForm(
        request.POST or None, instance=product.decision_config
    )
    if request.method == "POST" and sku_form.is_valid() and config_form.is_valid():
        with transaction.atomic():
            sku_form.save()
            config_form.save()
        messages.success(request, f"Perubahan {product.name} berhasil disimpan.")
        return redirect("dashboard:product_detail", sku_id=product.sku_id)
    return render(
        request,
        "dashboard/products/form.html",
        {
            "active_nav": "products",
            "sku_form": sku_form,
            "config_form": config_form,
            "product": product,
            "form_title": "Edit produk",
            "form_intro": "Perbarui profil produk, biaya, kapasitas, atau tenggat keputusan.",
        },
    )


def plan_list(request):
    drafts = ActionPlanDraft.objects.select_related("sku").all()
    return render(
        request,
        "dashboard/plans.html",
        {"active_nav": "plans", "drafts": drafts},
    )


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

    deadline = source.get("deadline")
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
        return redirect("dashboard:decision_center", sku_id=sku_id)

    sku = get_object_or_404(SKU, pk=sku_id)
    # Persist the exact scenario the user reviewed. Without forwarding these
    # values, approving a what-if result would silently save the default plan.
    overrides = _parse_overrides(request)
    result = Agent().run(sku, overrides=overrides)
    rec = result["decision"].recommended

    ActionPlanDraft.objects.create(
        sku=sku,
        approved_by=request.user,
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
    return redirect("dashboard:decision_center", sku_id=sku_id)
