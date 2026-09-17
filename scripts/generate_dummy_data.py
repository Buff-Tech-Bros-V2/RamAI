"""VIRALCAST synthetic data generator (MVP, hackathon scope).

Implements PRD sections 9.1 / 9.2 / 10.2 and requirements FR-D01..FR-D07.

Generation order (PRD 10.2):
    1. latent demand from base level, hour/day seasonality, trend, price, promo
    2. social-commerce events (content velocity, conversion lag, peak, decay)
    3. inventory constraint -> observed orders can be < latent demand
    4. cancel-before-ship / failed delivery / return simulated separately
    5. missing content signal and false viral spikes injected
    6. event seeds stored so episodes are reproducible and splittable

Outputs (default ./data):
    hourly_observations.{csv,parquet}  PRD 9.1 observable schema + split column
    training_labels.{csv,parquet}      cumulative fulfillment demand targets 24/48/72h
    latent_truth.csv                   EVALUATOR ONLY - never a feature
    events.csv                         one row per injected social-commerce episode
    decision_config.csv                PRD 9.2 decision configuration
    sku_master.csv                     static SKU attributes
    splits.json                        time + event-seed split boundaries
    metadata.json                      seed, versions, row counts, episode index

Usage:
    python scripts/generate_dummy_data.py --seed 42
    python scripts/generate_dummy_data.py --seed 7 --days 120 --out data_alt
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

DATASET_VERSION = "synthetic-v1"
TZ = timezone(timedelta(hours=7))  # Asia/Jakarta, explicit per NFR
DEFAULT_NOW = datetime(2026, 9, 17, 13, 0, tzinfo=TZ)

# --------------------------------------------------------------------------
# Static configuration
# --------------------------------------------------------------------------

HOUR_PROFILE = np.array(
    [0.25, 0.16, 0.11, 0.09, 0.10, 0.18, 0.38, 0.62, 0.85, 1.00, 1.12, 1.24,
     1.30, 1.18, 1.05, 1.02, 1.10, 1.28, 1.52, 1.78, 1.86, 1.55, 1.02, 0.52]
)
DOW_PROFILE = np.array([1.02, 0.96, 0.94, 0.98, 1.10, 1.28, 1.20])  # Mon..Sun


@dataclass
class SkuSpec:
    sku_id: str
    product_name: str
    product_category: str
    base_level: float            # mean latent units/hour before seasonality
    price: float
    unit_variable_cost: float
    price_elasticity: float
    overdispersion: float        # negative-binomial dispersion (higher = calmer)
    initial_stock: int
    replenishment_qty: int
    replenishment_interval_h: int
    production_minutes_per_unit: float
    material_per_unit: float
    minimum_commitment: int
    shelf_life_hours: int
    salvage_value_per_unit: float
    cancel_rate: float
    delivery_fail_rate: float
    return_rate: float
    affiliate_share: float       # share of orders attributable to affiliates at baseline
    views_per_order: float


SKUS: list[SkuSpec] = [
    SkuSpec("SKU-001", "Sambal Bawang Premium 200g", "packaged_food",
            base_level=3.1, price=38_000, unit_variable_cost=17_500,
            price_elasticity=1.35, overdispersion=6.0,
            initial_stock=260, replenishment_qty=180, replenishment_interval_h=48,
            production_minutes_per_unit=1.6, material_per_unit=1.0,
            minimum_commitment=20, shelf_life_hours=24 * 120, salvage_value_per_unit=9_000,
            cancel_rate=0.05, delivery_fail_rate=0.03, return_rate=0.04,
            affiliate_share=0.28, views_per_order=42.0),
    SkuSpec("SKU-002", "Keripik Singkong Pedas 150g", "packaged_food",
            base_level=1.7, price=24_000, unit_variable_cost=11_000,
            price_elasticity=1.10, overdispersion=4.0,
            initial_stock=150, replenishment_qty=110, replenishment_interval_h=48,
            production_minutes_per_unit=1.1, material_per_unit=0.8,
            minimum_commitment=25, shelf_life_hours=24 * 90, salvage_value_per_unit=5_500,
            cancel_rate=0.06, delivery_fail_rate=0.035, return_rate=0.05,
            affiliate_share=0.34, views_per_order=55.0),
    SkuSpec("SKU-003", "Kopi Susu Botol 250ml", "packaged_food",
            base_level=6.2, price=19_000, unit_variable_cost=9_500,
            price_elasticity=0.85, overdispersion=9.0,
            initial_stock=520, replenishment_qty=320, replenishment_interval_h=24,
            production_minutes_per_unit=0.7, material_per_unit=1.2,
            minimum_commitment=40, shelf_life_hours=24 * 30, salvage_value_per_unit=3_000,
            cancel_rate=0.04, delivery_fail_rate=0.025, return_rate=0.03,
            affiliate_share=0.22, views_per_order=31.0),
    SkuSpec("SKU-004", "Abon Sapi Original 100g", "packaged_food",
            base_level=0.9, price=52_000, unit_variable_cost=26_000,
            price_elasticity=1.55, overdispersion=3.0,
            initial_stock=90, replenishment_qty=70, replenishment_interval_h=72,
            production_minutes_per_unit=2.4, material_per_unit=1.5,
            minimum_commitment=15, shelf_life_hours=24 * 180, salvage_value_per_unit=14_000,
            cancel_rate=0.07, delivery_fail_rate=0.04, return_rate=0.06,
            affiliate_share=0.40, views_per_order=68.0),
    SkuSpec("SKU-005", "Bumbu Rendang Instan 120g", "packaged_food",
            base_level=2.3, price=31_000, unit_variable_cost=14_000,
            price_elasticity=1.20, overdispersion=5.0,
            initial_stock=200, replenishment_qty=140, replenishment_interval_h=48,
            production_minutes_per_unit=1.3, material_per_unit=1.0,
            minimum_commitment=20, shelf_life_hours=24 * 150, salvage_value_per_unit=7_500,
            cancel_rate=0.05, delivery_fail_rate=0.03, return_rate=0.045,
            affiliate_share=0.25, views_per_order=47.0),
]

SHOP_ID = "SHOP-DEMO-01"

SHOP_CONFIG = {
    "shop_id": SHOP_ID,
    "operation_mode": "PRODUCTION",
    "constraint_profile": "FOOD_DEMO",
    "daily_capacity_minutes": 480,
    "raw_material_stock": 900,
    "packaging_stock": 1_200,
    "working_capital_limit": 6_500_000,
}

# Episode archetypes (FR-D04). conversion_efficiency translates content velocity
# into actual demand uplift; a false signal converts almost nothing.
#
# ASSUMPTION -- affiliate conversion lag (lag_h). Content velocity leads the
# order response by 8-40 hours: a viewer saves or shares a video and buys later
# in the day or the next day, and affiliate reach keeps compounding after the
# original post. An earlier version of this generator used 1-8 hours, which made
# the content signal worthless by construction -- with 24-72h forecast horizons,
# a 2-hour head start carries no information that the transaction series does not
# already contain by the time of the cutoff. This value is a stated modelling
# assumption, not a measurement; it should be shown in the UI and revisited
# against real seller data in the PRD 21 Phase 1 pilot.
EPISODE_TYPES = {
    "TRUE_SURGE":     dict(peak_multiplier=(4.5, 7.5), ramp_h=(4, 8),   half_life_h=(40, 72), lag_h=(12, 30), conversion=(0.80, 1.00), content=True),
    "FADING_SURGE":   dict(peak_multiplier=(3.5, 6.0), ramp_h=(2, 5),   half_life_h=(5, 10),  lag_h=(8, 20),  conversion=(0.70, 0.95), content=True),
    "FALSE_CONTENT":  dict(peak_multiplier=(3.0, 6.5), ramp_h=(2, 5),   half_life_h=(6, 14),  lag_h=(12, 30), conversion=(0.02, 0.10), content=True),
    "MISSING_SIGNAL": dict(peak_multiplier=(3.0, 5.5), ramp_h=(3, 7),   half_life_h=(18, 40), lag_h=(12, 30), conversion=(0.75, 1.00), content=False),
    "SLOW_BURN":      dict(peak_multiplier=(1.8, 2.8), ramp_h=(10, 20), half_life_h=(50, 90), lag_h=(18, 40), conversion=(0.70, 0.95), content=True),
}


@dataclass
class EpisodeSpec:
    episode_id: str
    event_seed: int
    episode_type: str
    sku_id: str
    start_index: int             # hour offset from series start
    peak_multiplier: float
    ramp_hours: int
    half_life_hours: float
    conversion_lag_hours: int
    conversion_efficiency: float
    has_content_signal: bool
    split: str = ""
    duration_hours: int = 0
    capacity_conflict_group: str | None = None


@dataclass
class GeneratorConfig:
    seed: int = 42
    days: int = 90
    now: datetime = DEFAULT_NOW
    train_frac: float = 0.70
    val_frac: float = 0.15
    episodes_per_sku: tuple[int, int] = (3, 5)
    content_blackout_blocks: int = 6      # FR-D06 null-content stress blocks
    content_blackout_len: tuple[int, int] = (6, 30)
    horizons: tuple[int, ...] = (24, 48, 72)
    out_dir: str = "data"
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _u(rng: np.random.Generator, bounds) -> float:
    lo, hi = bounds
    return float(rng.uniform(lo, hi))


def _ui(rng: np.random.Generator, bounds) -> int:
    lo, hi = bounds
    return int(rng.integers(lo, hi + 1))


def negbin(rng: np.random.Generator, mu, dispersion: float) -> np.ndarray:
    """Negative-binomial draw parameterised by mean and dispersion (r)."""
    mu = np.maximum(np.asarray(mu, dtype=float), 1e-9)
    p = dispersion / (dispersion + mu)
    return rng.negative_binomial(dispersion, p)


def event_shape(n: int, start: int, ramp: int, half_life: float) -> np.ndarray:
    """Normalised 0..1 intensity curve: linear ramp to peak then exponential decay."""
    out = np.zeros(n, dtype=float)
    if start >= n:
        return out
    t = np.arange(n - start, dtype=float)
    ramp = max(ramp, 1)
    curve = np.where(t < ramp, t / ramp, np.exp(-np.log(2) * (t - ramp) / max(half_life, 0.5)))
    out[start:] = curve
    return out


# --------------------------------------------------------------------------
# Episode planning
# --------------------------------------------------------------------------

def plan_episodes(cfg: GeneratorConfig, n_hours: int, rng: np.random.Generator,
                  split_bounds: tuple[int, int]) -> list[EpisodeSpec]:
    """Lay out social-commerce episodes, keeping event seeds disjoint across splits."""
    train_end, val_end = split_bounds
    episodes: list[EpisodeSpec] = []
    seed_counter = int(rng.integers(100_000, 900_000))
    cycle = list(EPISODE_TYPES)

    # Every split gets the full mix of archetypes so the test set can exercise
    # fading surges, false content spikes and missing-signal fallback too.
    windows = {
        "train": (24 * 10, train_end - 96, cfg.episodes_per_sku[1]),
        "val": (train_end, val_end - 96, 1),
        "test": (val_end, n_hours - 96, 2),
    }

    for si, sku in enumerate(SKUS):
        k = 0
        for split_name, (lo, hi, n_ep) in windows.items():
            if hi <= lo:
                continue
            starts = np.sort(rng.choice(np.arange(lo, hi), size=min(n_ep, hi - lo), replace=False))
            kept: list[int] = []
            for s in starts:
                if not kept or s - kept[-1] >= 72:   # >= 3 days between episodes of one SKU
                    kept.append(int(s))
            for start in kept:
                etype = cycle[(si + k) % len(cycle)]
                spec = EPISODE_TYPES[etype]
                seed_counter += 1
                k += 1
                ep_rng = np.random.default_rng(seed_counter)
                half_life = _u(ep_rng, spec["half_life_h"])
                ramp = _ui(ep_rng, spec["ramp_h"])
                lag = _ui(ep_rng, spec["lag_h"])
                episodes.append(EpisodeSpec(
                    episode_id=f"EP-{sku.sku_id[-3:]}-{k:02d}",
                    event_seed=seed_counter,
                    episode_type=etype,
                    sku_id=sku.sku_id,
                    start_index=start,
                    peak_multiplier=_u(ep_rng, spec["peak_multiplier"]),
                    ramp_hours=ramp,
                    half_life_hours=half_life,
                    conversion_lag_hours=lag,
                    conversion_efficiency=_u(ep_rng, spec["conversion"]),
                    has_content_signal=spec["content"],
                    # the label window covers the lagged demand response, not just
                    # the content curve, so per-episode metrics do not score real
                    # surge hours as NORMAL once the conversion lag pushes the
                    # order response later than the content spike
                    duration_hours=int(ramp + 5 * half_life + lag),
                    split=split_name,
                ))

    # FR-D04 capacity conflict: force two SKUs to surge together inside the test window
    conflict_start = int(val_end + (n_hours - val_end) * 0.45)
    for j, sku in enumerate(SKUS[:2]):
        seed_counter += 1
        ep_rng = np.random.default_rng(seed_counter)
        episodes.append(EpisodeSpec(
            episode_id=f"EP-CONFLICT-{j + 1:02d}",
            event_seed=seed_counter,
            episode_type="TRUE_SURGE",
            sku_id=sku.sku_id,
            start_index=conflict_start + j * 2,
            peak_multiplier=_u(ep_rng, (5.0, 7.0)),
            ramp_hours=_ui(ep_rng, (4, 7)),
            half_life_hours=_u(ep_rng, (36, 60)),
            conversion_lag_hours=_ui(ep_rng, (12, 24)),
            conversion_efficiency=_u(ep_rng, (0.85, 1.0)),
            has_content_signal=True,
            duration_hours=96,
            split="test",
            capacity_conflict_group="CONFLICT-A",
        ))
    return episodes


# --------------------------------------------------------------------------
# Core simulation, one SKU
# --------------------------------------------------------------------------

def simulate_sku(sku: SkuSpec, cfg: GeneratorConfig, index: pd.DatetimeIndex,
                 episodes: list[EpisodeSpec], rng: np.random.Generator):
    n = len(index)
    hours = index.hour.to_numpy()
    dows = index.dayofweek.to_numpy()

    # --- 1. latent demand ------------------------------------------------
    trend = 1.0 + 0.00012 * np.arange(n)
    weekly_wave = 1.0 + 0.06 * np.sin(2 * np.pi * np.arange(n) / (24 * 7))

    # price path: base price with occasional multi-day discounts
    price = np.full(n, sku.price, dtype=float)
    promo = np.zeros(n, dtype=int)
    n_promos = max(1, cfg.days // 12)
    promo_starts = rng.choice(np.arange(0, max(n - 72, 1)), size=n_promos, replace=False)
    for ps in promo_starts:
        dur = int(rng.integers(24, 73))
        disc = float(rng.uniform(0.08, 0.25))
        price[ps:ps + dur] = round(sku.price * (1 - disc), -2)
        promo[ps:ps + dur] = 1

    mu = (sku.base_level * HOUR_PROFILE[hours] * DOW_PROFILE[dows] * trend * weekly_wave
          * (price / sku.price) ** (-sku.price_elasticity)
          * (1.0 + 0.22 * promo))

    # --- 2. social-commerce events --------------------------------------
    sku_eps = [e for e in episodes if e.sku_id == sku.sku_id]
    content_intensity = np.zeros(n)     # drives the observable content signal
    demand_multiplier = np.ones(n)      # drives latent demand, lagged vs content
    episode_id_col = np.array([""] * n, dtype=object)
    episode_type_col = np.array([""] * n, dtype=object)

    for ep in sku_eps:
        shape = event_shape(n, ep.start_index, ep.ramp_hours, ep.half_life_hours)
        if ep.has_content_signal:
            content_intensity = np.maximum(content_intensity, shape)
        lagged = np.roll(shape, ep.conversion_lag_hours)
        lagged[:ep.conversion_lag_hours] = 0.0
        uplift = 1.0 + (ep.peak_multiplier - 1.0) * ep.conversion_efficiency * lagged
        demand_multiplier = np.maximum(demand_multiplier, uplift)
        end = min(n, ep.start_index + ep.duration_hours)
        episode_id_col[ep.start_index:end] = ep.episode_id
        episode_type_col[ep.start_index:end] = ep.episode_type

    mu_latent = mu * demand_multiplier
    latent_demand = negbin(rng, mu_latent, sku.overdispersion)

    # --- 3. inventory constraint ----------------------------------------
    stock = np.zeros(n, dtype=int)
    incoming = np.zeros(n, dtype=int)
    orders_created = np.zeros(n, dtype=int)
    stockout_flag = np.zeros(n, dtype=int)

    # The planner only sees *past observed* sales and keeps a thin coverage buffer,
    # so a surge outruns replenishment and produces genuine stockout censoring.
    on_hand = sku.initial_stock
    interval = sku.replenishment_interval_h
    for t in range(n):
        if t > 0 and t % interval == 0:
            recent = orders_created[max(0, t - interval):t].sum()
            baseline = sku.base_level * HOUR_PROFILE.mean() * interval
            planned = max(recent, baseline * 0.8) * float(rng.uniform(1.0, 1.15))
            qty = int(max(rng.normal(planned, planned * 0.10), 0))
            if rng.random() < 0.12:          # supplier shortfall / late batch
                qty = int(qty * rng.uniform(0.35, 0.7))
            arrival = t if rng.random() > 0.15 else min(t + int(rng.integers(4, 13)), n - 1)
            incoming[arrival] += qty
        on_hand += incoming[t]
        stock[t] = on_hand
        served = int(min(latent_demand[t], on_hand))
        orders_created[t] = served
        if served < latent_demand[t] or on_hand == 0:
            stockout_flag[t] = 1
        on_hand -= served

    # --- 4. fulfillment outcomes (separate processes, with lag) ----------
    cancelled = rng.binomial(orders_created, sku.cancel_rate)
    shipped_at_order = orders_created - cancelled

    ship_lag, deliver_lag, return_lag = 2, 24, 72
    shipped = np.zeros(n, dtype=int)
    delivered = np.zeros(n, dtype=int)
    returned = np.zeros(n, dtype=int)
    for t in range(n):
        s = int(shipped_at_order[t])
        if s == 0:
            continue
        shipped[min(t + ship_lag, n - 1)] += s
        ok = s - int(rng.binomial(s, sku.delivery_fail_rate))
        delivered[min(t + deliver_lag, n - 1)] += ok
        if ok > 0:
            returned[min(t + return_lag, n - 1)] += int(rng.binomial(ok, sku.return_rate))

    # --- 5. content signal + missing blocks ------------------------------
    base_affiliate = orders_created * sku.affiliate_share
    affiliate_orders = rng.binomial(
        orders_created,
        np.clip(sku.affiliate_share + 0.45 * content_intensity, 0.0, 0.95),
    )
    views_mu = (orders_created * sku.views_per_order * (1 + 0.15 * rng.normal(size=n))
                + 900 * content_intensity * sku.views_per_order * 0.12 + 5)
    product_views = negbin(rng, np.maximum(views_mu, 1.0), 12.0)
    content_view_velocity = negbin(rng, 60 + 5200 * content_intensity + 8 * base_affiliate, 8.0).astype(float)
    active_affiliates = negbin(rng, 1.5 + 22 * content_intensity + 0.1 * base_affiliate, 6.0)

    content_cols = {
        "product_views": product_views.astype(float),
        "affiliate_orders": affiliate_orders.astype(float),
        "active_affiliates": active_affiliates.astype(float),
        "content_view_velocity": content_view_velocity,
    }
    content_missing = np.zeros(n, dtype=int)

    # episodes flagged as MISSING_SIGNAL hide their content entirely
    for ep in sku_eps:
        if ep.has_content_signal:
            continue
        end = min(n, ep.start_index + ep.duration_hours)
        content_missing[ep.start_index:end] = 1

    # plus random blackout blocks anywhere in the series (FR-D06)
    for _ in range(cfg.content_blackout_blocks):
        start = int(rng.integers(0, max(n - 48, 1)))
        content_missing[start:start + _ui(rng, cfg.content_blackout_len)] = 1

    for col, values in content_cols.items():
        v = values.astype(float).copy()
        v[content_missing == 1] = np.nan
        content_cols[col] = v

    frame = pd.DataFrame({
        "timestamp": index,
        "shop_id": SHOP_ID,
        "sku_id": sku.sku_id,
        "product_category": sku.product_category,
        "orders_created": orders_created,
        "orders_cancelled_pre_ship": cancelled,
        "orders_shipped": shipped,
        "orders_delivered": delivered,
        "orders_returned": returned,
        "stock_on_hand": stock,
        "incoming_stock": incoming,
        "stockout_flag": stockout_flag,
        "price": price,
        "promotion_flag": promo,
        **content_cols,
        "operation_mode": SHOP_CONFIG["operation_mode"],
        "constraint_profile": SHOP_CONFIG["constraint_profile"],
    })

    truth = pd.DataFrame({
        "timestamp": index,
        "sku_id": sku.sku_id,
        "latent_demand": latent_demand,
        "latent_mu": np.round(mu_latent, 4),
        "baseline_mu": np.round(mu, 4),
        "demand_multiplier": np.round(demand_multiplier, 4),
        "content_intensity": np.round(content_intensity, 4),
        "censored_units": latent_demand - orders_created,
        "episode_id": episode_id_col,
        "episode_type": episode_type_col,
    })
    return frame, truth


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def build_labels(obs: pd.DataFrame, horizons) -> pd.DataFrame:
    """Cumulative fulfillment demand after the forecast cutoff (PRD 10.2).

    fulfillment_demand(t) = orders_created(t) - orders_cancelled_pre_ship(t)
    Windows overlapping a stockout hour are censored: the label understates true
    demand, so it is flagged and must be dropped or reweighted during training.
    """
    parts = []
    for _, g in obs.sort_values("timestamp").groupby("sku_id", sort=False):
        g = g.reset_index(drop=True)
        fulfillment = (g["orders_created"] - g["orders_cancelled_pre_ship"]).astype(float)
        out = g[["timestamp", "sku_id"]].copy()
        out["fulfillment_demand"] = fulfillment
        rev_full = fulfillment.iloc[::-1]
        rev_out = g["stockout_flag"].astype(float).iloc[::-1]
        for h in horizons:
            fwd = rev_full.rolling(h, min_periods=h).sum().iloc[::-1].shift(-1)
            censored = rev_out.rolling(h, min_periods=h).max().iloc[::-1].shift(-1)
            out[f"target_demand_{h}h"] = fwd.to_numpy()
            out[f"target_censored_{h}h"] = censored.to_numpy()
        parts.append(out)
    labels = pd.concat(parts, ignore_index=True)
    for h in horizons:
        labels[f"target_censored_{h}h"] = labels[f"target_censored_{h}h"].astype("Int64")
    return labels


def assign_splits(obs: pd.DataFrame, index: pd.DatetimeIndex, train_end: int, val_end: int):
    t_train, t_val = index[train_end], index[val_end]
    return np.where(obs["timestamp"] < t_train, "train",
                    np.where(obs["timestamp"] < t_val, "val", "test"))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def generate(cfg: GeneratorConfig) -> dict:
    n_hours = cfg.days * 24
    end = cfg.now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=n_hours - 1)
    index = pd.date_range(start, end, freq="h", tz=TZ)

    train_end = int(n_hours * cfg.train_frac)
    val_end = int(n_hours * (cfg.train_frac + cfg.val_frac))

    master_rng = np.random.default_rng(cfg.seed)
    episodes = plan_episodes(cfg, n_hours, master_rng, (train_end, val_end))

    obs_parts, truth_parts = [], []
    for i, sku in enumerate(SKUS):
        sku_rng = np.random.default_rng([cfg.seed, i])
        frame, truth = simulate_sku(sku, cfg, index, episodes, sku_rng)
        obs_parts.append(frame)
        truth_parts.append(truth)

    obs = pd.concat(obs_parts, ignore_index=True)
    truth = pd.concat(truth_parts, ignore_index=True)
    obs["split"] = assign_splits(obs, index, train_end, val_end)
    truth["split"] = obs["split"].to_numpy()

    labels = build_labels(obs, cfg.horizons)
    labels = labels.merge(obs[["timestamp", "sku_id", "split"]], on=["timestamp", "sku_id"], how="left")

    events = pd.DataFrame([asdict(e) for e in episodes])
    events["event_start"] = [index[e.start_index] for e in episodes]

    sku_master = pd.DataFrame([asdict(s) for s in SKUS])

    decision_config = pd.DataFrame([{
        "operation_mode": SHOP_CONFIG["operation_mode"],
        "constraint_profile": SHOP_CONFIG["constraint_profile"],
        "sku_id": s.sku_id,
        "unit_selling_price": s.price,
        "unit_variable_cost": s.unit_variable_cost,
        "production_minutes_per_unit": s.production_minutes_per_unit,
        "material_per_unit": s.material_per_unit,
        "supplier_lead_time_hours": None,          # null under PRODUCTION mode
        "minimum_commitment": s.minimum_commitment,
        "shelf_life_hours": s.shelf_life_hours,
        "salvage_value_per_unit": s.salvage_value_per_unit,
        "shop_id": SHOP_CONFIG["shop_id"],
        "daily_capacity_minutes": SHOP_CONFIG["daily_capacity_minutes"],
        "raw_material_stock": SHOP_CONFIG["raw_material_stock"],
        "packaging_stock": SHOP_CONFIG["packaging_stock"],
        "working_capital_limit": SHOP_CONFIG["working_capital_limit"],
        "commitment_deadline": (cfg.now + timedelta(hours=3)).isoformat(),
    } for s in SKUS])

    splits = {
        "strategy": "time-based, event seeds disjoint across splits",
        "train": {"start": index[0].isoformat(), "end": index[train_end - 1].isoformat()},
        "val": {"start": index[train_end].isoformat(), "end": index[val_end - 1].isoformat()},
        "test": {"start": index[val_end].isoformat(), "end": index[-1].isoformat()},
        "event_seeds": {s: sorted(events.loc[events["split"] == s, "event_seed"].tolist())
                        for s in ("train", "val", "test")},
    }

    # FR-D05 guard: an event seed must never appear in two splits
    seen: dict[int, str] = {}
    for split_name, seeds in splits["event_seeds"].items():
        for sd in seeds:
            if sd in seen:
                raise AssertionError(f"event seed {sd} leaks between {seen[sd]} and {split_name}")
            seen[sd] = split_name

    return {
        "observations": obs,
        "labels": labels,
        "latent_truth": truth,
        "events": events,
        "sku_master": sku_master,
        "decision_config": decision_config,
        "splits": splits,
        "index": index,
    }


def write_outputs(result: dict, cfg: GeneratorConfig) -> Path:
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    obs, labels = result["observations"], result["labels"]
    obs.to_csv(out / "hourly_observations.csv", index=False)
    obs.to_parquet(out / "hourly_observations.parquet", index=False)
    labels.to_csv(out / "training_labels.csv", index=False)
    labels.to_parquet(out / "training_labels.parquet", index=False)
    result["latent_truth"].to_csv(out / "latent_truth.csv", index=False)
    result["events"].to_csv(out / "events.csv", index=False)
    result["sku_master"].to_csv(out / "sku_master.csv", index=False)
    result["decision_config"].to_csv(out / "decision_config.csv", index=False)
    (out / "splits.json").write_text(json.dumps(result["splits"], indent=2), encoding="utf-8")

    meta = {
        "dataset_version": DATASET_VERSION,
        "generated_at": datetime.now(TZ).isoformat(),
        "seed": cfg.seed,
        "days": cfg.days,
        "timezone": "Asia/Jakarta (+07:00)",
        "now_cutoff": cfg.now.isoformat(),
        "n_skus": len(SKUS),
        "n_rows": int(len(obs)),
        "horizons": list(cfg.horizons),
        "synthetic": True,
        "notice": "Synthetic Demo Data - latent_truth.csv is evaluator-only and must never be used as a feature.",
        "episode_counts": result["events"]["episode_type"].value_counts().to_dict(),
        "stockout_share": float(obs["stockout_flag"].mean()),
        "content_missing_share": float(obs["content_view_velocity"].isna().mean()),
        "rows_per_split": obs["split"].value_counts().to_dict(),
    }
    (out / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return out


def summarise(result: dict, out: Path) -> None:
    obs, labels, events = result["observations"], result["labels"], result["events"]
    print(f"\nWrote {len(obs):,} hourly rows for {obs['sku_id'].nunique()} SKUs -> {out.resolve()}")
    print("\nRows per split:")
    print(obs["split"].value_counts().to_string())
    print("\nEpisodes per type / split:")
    print(pd.crosstab(events["episode_type"], events["split"]).to_string())
    print("\nPer-SKU summary:")
    agg = obs.groupby("sku_id").agg(
        mean_orders=("orders_created", "mean"),
        max_orders=("orders_created", "max"),
        stockout_rate=("stockout_flag", "mean"),
        content_null_rate=("content_view_velocity", lambda s: s.isna().mean()),
    ).round(3)
    print(agg.to_string())
    print("\nLabel availability (non-null, uncensored):")
    for h in (24, 48, 72):
        col, cen = f"target_demand_{h}h", f"target_censored_{h}h"
        usable = labels[col].notna() & (labels[cen] == 0)
        print(f"  {h}h: {labels[col].notna().sum():>6,} labelled, {usable.sum():>6,} uncensored "
              f"({usable.mean() * 100:.1f}%), median={labels.loc[usable, col].median():.1f}")


def main() -> None:
    p = argparse.ArgumentParser(description="Generate VIRALCAST synthetic training/test data.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", default="data")
    p.add_argument("--now", default=DEFAULT_NOW.isoformat(),
                   help="ISO timestamp treated as the last hour of history")
    args = p.parse_args()

    cfg = GeneratorConfig(seed=args.seed, days=args.days, out_dir=args.out,
                          now=datetime.fromisoformat(args.now).astimezone(TZ))
    result = generate(cfg)
    out = write_outputs(result, cfg)
    summarise(result, out)


if __name__ == "__main__":
    main()
