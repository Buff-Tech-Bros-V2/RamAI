# RamAI

RamAI is a decision-support tool for TikTok/e-commerce sellers: given a
product's order history (and optionally content/affiliate signals), it
forecasts near-term demand, turns that forecast into three costed
restocking options (commit now / stage it / wait), and explains the pick
in plain Indonesian. A human always approves before anything is "ordered" —
the MVP never places a real purchase.

Full product spec: [RamAI_PRD.md](RamAI_PRD.md). Visual/brand spec: [design.md](design.md).
ML track handoff notes (metrics, open items, traps): [PROGRESS.md](PROGRESS.md).

## What actually happens when you load a product's page

```
HourlyObservation history  →  ForecastProvider.run_forecast()  →  ForecastOutput (P10/P50/P90, surge persistence)
                                                                          │
                                                                          ▼
DecisionConfig (costs/capacity/capital)  →  DecisionEngine.evaluate_actions()  →  DecisionResult (recommended + 2 alternatives)
                                                                          │
                                                                          ▼
                              ExplanationPacket  →  LLMExplainer.explain()  →  Markdown narrative (only on request)
```

`apps.agent.orchestrator.Agent.run()` wires all four steps together and is
the only entry point the dashboard calls. Forecast + decision always run;
the LLM call is opt-in (`explain=True`) because it's the one rate-limited,
non-deterministic step — the dashboard only asks for it when a seller clicks
"Analisis".

## Module map

Each concern is a separate Django app so pieces can be developed and swapped independently:

| App | Responsibility | Status |
| --- | --- | --- |
| `apps/skus` | `SKU`, `DecisionConfig` (constraints/economics), `HourlyObservation` history models; `seed_dummy_data` / `seed_fashion_data` management commands | Real, multi-tenant |
| `apps/forecasting` | `ForecastProvider` interface; trained LightGBM quantile pipeline (`LightGBMForecastProvider`) with a heuristic fallback (`DummyForecastProvider`) | Real (trained model) |
| `apps/decisionengine` | Turns a `ForecastOutput` into 3 candidate actions (commit now / staged / wait) + a "buy nothing" baseline, scored with the PRD §11 contribution formula; persists approved plans as `ActionPlanDraft` | Real logic |
| `apps/agent` | Orchestrates validate → forecast → decide → explain; `LLMExplainer` interface with Groq / Gemini / template implementations | Real |
| `apps/dashboard` | Views + templates: login-gated multi-tenant UI, product CRUD, decision center with what-if sliders, plan drafts, HTMX partials | Real |

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser        # you need an account to log in — every view requires auth
python manage.py seed_fashion_data      # creates its own "fashion_seller" user + 5 SKUs + 90d history
python manage.py runserver
```

Open <http://127.0.0.1:8000/> and log in. Log in as `fashion_seller` /
`fashion123` (created by the command above) to see seeded products, or log
in as your superuser and add products by hand via "Tambah produk".

> **`seed_dummy_data` is currently broken on a fresh database.** `SKU.owner`
> became a required field when per-seller login was added
> (`apps/skus/migrations/0003_sku_owner.py`), but `seed_dummy_data` (both its
> sine-wave path and its "load `data/*.parquet`" path) never sets `owner` on
> the SKUs it creates, so it fails with `IntegrityError: NOT NULL constraint
> failed: skus_sku.owner_id` — even if a superuser already exists. Use
> `seed_fashion_data` instead (it creates and owns its own user), or add
> products manually through the UI. See "Known issues" below.

To train a real forecast model instead of relying on the fallback heuristic:

```bash
python scripts/generate_dummy_data.py --seed 42   # writes data/*.csv, *.parquet
python scripts/train_forecast.py                  # writes artifacts/ (gitignored)
```

`apps.forecasting.services.get_forecast_provider()` auto-detects trained
artifacts under `artifacts/forecast_transaction_content/` and serves the
LightGBM provider when present, falling back to `DummyForecastProvider`
otherwise — the app boots either way.

## Data model (`apps/skus/models.py`)

- **`SKU`** — one product, scoped to an `owner` (Django `User`). `product_category`,
  `shop_id`, and an optional `demo_scenario` label used by the seed commands.
- **`DecisionConfig`** — one-to-one with `SKU`. Everything the decision engine
  needs: `operation_mode` (`PRODUCTION` — you manufacture; `REPLENISHMENT` —
  you order from a supplier), a `constraint_profile` preset (Food/Fashion/
  Beauty/Electronics — see `CONSTRAINT_PROFILE_PRESETS`) that pre-fills
  sensible defaults, prices/costs, `minimum_commitment` (MOQ),
  `shelf_life_hours`, `salvage_value_per_unit`, `daily_capacity_minutes`
  (production mode only), `supplier_lead_time_hours` (replenishment mode
  only), `working_capital_limit`, and a rolling `decision_window_hours`
  (hours from *now*, not a fixed timestamp — so it never goes stale).
- **`HourlyObservation`** — one row per SKU per hour: orders
  created/cancelled/shipped/delivered/returned, `stock_on_hand`,
  `stockout_flag`, `price`/`promotion_flag`, and nullable content signals
  (`product_views`, `affiliate_orders`, `active_affiliates`,
  `content_view_velocity`) — nullable because a seller with no
  affiliate/content program must still get a forecast (FR-D06).

`apps/decisionengine/models.py` adds **`ActionPlanDraft`**: a snapshot of
whichever recommendation a seller approved. The MVP never executes it —
approving just records "a human signed off on this plan" (PRD FR-A05).

## Forecasting (`apps/forecasting/`)

`ForecastProvider.run_forecast(sku, cutoff, horizons_hours)` is the contract
everything downstream depends on; whichever implementation is behind it,
callers only ever see `ForecastOutput` (`apps/forecasting/dataclasses.py`):
P10/P50/P90 for cumulative demand over the horizon, a
`surge_persistence_probability`, `content_features_used`, and
`data_quality_flags`.

Two implementations, selected by `get_forecast_provider()`:

- **`DummyForecastProvider`** (`services.py`) — a heuristic placeholder: it
  derives P10/P50/P90 from a 24h vs 6–8-day-ago moving average and estimates
  surge persistence from the ratio between them. Used whenever no trained
  artifacts exist, or ML dependencies (`lightgbm`/`numpy`/`pandas`) aren't
  installed — the web app always boots.
- **`LightGBMForecastProvider`** (`provider.py`) — the real model, trained
  by `scripts/train_forecast.py`:
  1. `features.py` builds a leakage-safe feature row at the cutoff (lag/rolling
     order stats, content-block features when available; ~48 columns
     transaction-only, ~73 with content).
  2. `quantile_model.py`'s `QuantileBundle` predicts P10/P50/P90 per horizon
     with one LightGBM booster per (horizon, quantile), fit on a **ratio
     target** (demand ÷ trailing level, not the raw count — trees can't
     extrapolate past training leaves, so a ratio target is what lets the
     model say "3x normal" for a SKU it's never seen that busy), then widens
     the interval with a per-horizon **conformal correction** fitted on the
     validation split.
  3. It also fits a native LightGBM **persistence classifier** per horizon;
     `provider.py` falls back to a **block-bootstrap** over empirical
     residuals (`scenarios.py`) when no classifier is saved for that horizon.
  4. Missing/short history is reported through `data_quality_flags`
     (`insufficient_history`, `no_observations`, `content_signal_missing`)
     rather than crashing — a tool failure must never produce a fabricated
     recommendation.

Training pipeline (owned by `scripts/`, artifacts gitignored — retrain
locally before you need them):

| Script | Purpose |
| --- | --- |
| `scripts/generate_dummy_data.py --seed N` | Reproducible synthetic generator (PRD §10.2): latent demand → social-commerce events → inventory-constrained observed orders → cancel/fail/return simulation. Same seed = byte-identical output. Writes `data/`. |
| `scripts/train_forecast.py` | Trains `seasonal_naive`/`moving_average` baselines plus both quantile bundles (transaction-only and transaction+content) on identical rows, scores all on the same held-out split, writes `artifacts/`. |
| `scripts/evaluate_persistence.py` | Scores whether content predicts surge *persistence* specifically (not just point accuracy). |
| `scripts/multiseed_comparison.py --seeds ...` | Re-runs the transaction-vs-content comparison across several dataset seeds — a single seed has flipped which one "wins" before. |
| `scripts/test_manual_forecast.py` | Ad-hoc CLI to run a trained bundle against a specific SKU/cutoff, or compare a calm vs. surge period. |

`apps/forecasting/services.py` serves the `transaction_content` bundle by
default (`FORECAST_FEATURE_MODE`). The rest of this section explains what
that model actually is, what it's fed, how it's trained, and what its
scores mean. Full numbers, caveats, and open items are in
[`data/README.md`](data/README.md) and [`PROGRESS.md`](PROGRESS.md).

### The algorithm

**LightGBM** (gradient-boosted decision trees), used three different ways
inside one `QuantileBundle`:

1. **Quantile regression** — for each forecast horizon (24h/48h/72h), three
   separate boosters are trained, one per quantile (P10, P50, P90), using
   LightGBM's `objective: "quantile"` loss. A quantile loss just penalizes
   over- and under-prediction asymmetrically: the P90 booster is penalized
   9× more for predicting *too low* than *too high*, so it naturally learns
   to sit near the top of the demand distribution instead of the middle.
   That's what produces three different curves from the same features
   instead of one line plus a made-up margin. **9 boosters total** (3
   horizons × 3 quantiles).
2. **Binary classification** — one more booster per horizon (`objective:
   "binary"`) predicts the probability that a demand surge will still be
   running (see "Surge persistence" below), instead of deriving that
   probability indirectly from the quantile spread.
3. **Everything is pooled across SKUs.** There's no "one model per
   product" — every SKU's history goes into the same training set, and
   `sku_id`/`product_category` are just two more input columns (as integer
   codes). This lets a brand-new SKU with only a few days of history still
   borrow patterns learned from other SKUs, and it's why a single trained
   bundle in `artifacts/` serves every product in the app.

Why gradient-boosted trees and not, say, a neural net or ARIMA: the
dataset is small (a few thousand rows per horizon after filtering), mixes
continuous and categorical signals (price, calendar, SKU identity), and
needs quantile output out of the box — LightGBM supports all of that
natively and trains in seconds on a laptop, which matters for a 24-hour
hackathon iteration loop.

### The two things that matter more than the model

- **Ratio target, not a raw unit count.** Each booster is fit on
  `demand ÷ (trailing_level × horizon)` — "how many times the SKU's normal
  rate is this" — and the prediction is multiplied back into units at
  inference time (`quantile_model.target_scale`, blending the 24h and 72h
  rolling-mean order rate as "trailing level"). A tree can only output
  averages of values it saw during training, so on a raw unit count it has
  no way to say "3× normal" for a SKU whose normal level it's never seen
  that high — it just caps out. On the ratio scale, "3× normal" is a
  number the model has seen for *every* SKU, just at different absolute
  levels, so it generalizes. This is what took the under-forecast bias
  from +13% to +4% (training split) and improved accuracy at every horizon.
- **Conformal calibration, not a hand-picked margin.** Raw P10–P90 boosters
  only captured ~74% of actual outcomes inside their band (target: 80%).
  `_conformal_width()` fixes this the standard statistical way (split
  conformal / CQR): on the validation split — never the test split — it
  measures how far outside the raw P10/P90 band each actual value fell,
  takes the 80th percentile of that "miss distance," and widens the outer
  two quantiles by exactly that much. P50 is left untouched, since
  widening it would reintroduce bias rather than fix coverage.

### Indicators / features fed to the model (`apps/forecasting/features.py`)

Every feature is built strictly from data at or before the forecast
cutoff — nothing after it — so training and live inference can't leak the
future. There are two feature sets:

- **`transaction`** (~48 columns) — everything below except the content block.
- **`transaction_content`** (~73 columns) — adds the content/affiliate block.
  A seller with no affiliate program still gets a forecast: those columns
  are left `NaN` (LightGBM handles missing values natively) and
  `content_features_used` records whether a row actually had a signal.

| Feature group | Examples | What it tells the model |
| --- | --- | --- |
| **Lags** | `orders_created_lag_1/2/3/6/12/24/48/72/168` (and the same for `fulfillment_demand`) | "What did demand look like exactly N hours ago?" Lag 168 = same hour, one week ago, to capture weekly seasonality directly. |
| **Rolling mean / std** | `*_rollmean_6/24/72`, `*_rollstd_6/24/72` | The recent *average* level and *volatility* over the last 6h/24h/72h — smooths out single noisy hours so the model reacts to a trend, not a blip. |
| **Growth ratios** | `*_growth_24` (6h avg ÷ 24h avg), `*_growth_72` (24h avg ÷ 72h avg) | Is demand accelerating or decelerating right now, independent of the SKU's absolute size? This is the model's main "something is happening" signal. |
| **Calendar** | `hour`, `dow`, `hour_sin/cos`, `dow_sin/cos`, `is_weekend` | Hour-of-day and day-of-week, encoded as sine/cosine pairs so the model sees "23:00 and 00:00 are one hour apart," not 23 units apart. |
| **Price / promo** | `price`, `price_vs_trailing_mean`, `promotion_flag` | Whether the SKU is currently discounted relative to its own recent price — demand elasticity signal. |
| **Stockout signal** | `stockout_flag`, `stockout_share_24` | Was the shelf empty recently? Tells the model the *observed* order count under-counts *true* demand for that stretch, rather than treating "sold zero" as "wanted zero." (`stock_on_hand` itself was tried and deliberately removed — see below.) |
| **Identity** | `sku_id_code`, `product_category_code` | Lets one pooled model still learn SKU-specific baselines instead of averaging every product together. |
| **Content block** *(content mode only)* | `product_views`, `affiliate_orders`, `active_affiliates`, `content_view_velocity` + their lags (1/3/6/24/48h), `content_view_velocity_rollmean_6`, `content_view_velocity_growth_6`, `affiliate_order_share`, `views_per_order`, `content_features_used` | The early-warning signal: content/affiliate activity is assumed to lead orders by hours to a couple of days (see `data/README.md` for why), so a view/affiliate spike can flag a surge before the order count itself moves. |

**Deliberately excluded:** `stock_on_hand` / stock-cover-hours. It looked
useful (30% of total feature "gain") but it was actually teaching the
model the *replenishment rule* the data generator uses, not real demand —
removing it improved WAPE at every horizon. This is the kind of thing a
feature-importance sanity check (`quantile_model.feature_importance()`)
is there to catch.

### Hyperparameters (`quantile_model.py`)

| Parameter | Value | What it controls |
| --- | --- | --- |
| `objective` | `quantile` (P10/P50/P90 boosters), `binary` (persistence booster) | The loss function LightGBM optimizes — asymmetric pinball loss for quantiles, log-loss for the yes/no persistence classifier. |
| `learning_rate` | 0.05 (0.02 for the P90 booster) | How much each new tree corrects the previous ones. Lower = slower but more stable learning; the P90 booster gets an extra-slow rate because its asymmetric loss is easy to overfit early. |
| `num_leaves` | 31 (15 for P90 and the persistence booster) | Maximum complexity per tree. Fewer leaves = simpler trees = less overfitting, used for the harder-to-fit tails. |
| `min_data_in_leaf` | 40 (50 for P90) | Minimum training rows a leaf must cover before LightGBM will split on it — a floor against fitting noise from a handful of rows. |
| `feature_fraction` / `bagging_fraction` | 0.85 / 0.85 | Each tree sees a random 85% of columns/rows — standard bagging-style regularization so no single feature or SKU dominates every tree. |
| `lambda_l2` | 1.0 | L2 regularization on leaf weights — shrinks extreme leaf values toward zero. |
| `num_boost_round` / `early_stopping_rounds` | 600 / 50 | Train up to 600 trees, but stop as soon as 50 rounds pass with no improvement on the validation split — lets the data decide the real tree count instead of guessing one. |
| `seed` | 42 | Makes tree-building and row/feature sampling reproducible. |

### How training works, step by step

1. `scripts/generate_dummy_data.py --seed 42` produces 90 days of hourly
   history for 5 SKUs with labelled demand episodes, split 70/15/15 into
   train/validation/test **by time** (not randomly — a model must never
   train on data from after the moment it's predicting).
2. `scripts/train_forecast.py` calls `quantile_model.train_bundle()` once
   per feature mode (`transaction`, `transaction_content`). For each of the
   3 horizons:
   - Build the feature matrix (`features.build_training_frame`), dropping
     rows whose label window overlaps a stockout (`drop_censored=True`) —
     otherwise the model would learn "empty shelf ⇒ zero demand."
   - Convert the raw demand label to the ratio target described above.
   - Train the P10/P50/P90 boosters on the **train** split, with the
     **validation** split driving early stopping (never the test split).
   - Fit the conformal width correction on the **validation** split.
   - Fit the persistence classifier: label each training window `1` if its
     realized hourly rate is ≥1.2× the SKU's own 14-day trailing baseline,
     else `0`, then train a binary LightGBM classifier the same way.
   - Extract **empirical residual blocks** (6-hour chunks of actual÷predicted
     ratio) from validation predictions — these feed the block-bootstrap
     scenario generator (`scenarios.py`) that estimates surge persistence
     when no persistence classifier is available for a horizon.
3. Everything is evaluated **once**, at the end, on the untouched **test**
   split — same rows, same metrics, for the baselines and both quantile
   bundles — so the comparison in the tables below is apples-to-apples.
4. The trained bundle is written to `artifacts/forecast_<mode>/`: one
   `.txt` LightGBM model file per (horizon, quantile) plus the persistence
   classifiers, residual blocks, and a `bundle.json` manifest.

### What the scores mean (metric glossary)

| Metric | Plain-English meaning |
| --- | --- |
| **WAPE** (weighted absolute % error) | Total forecast error as a % of total actual demand. Lower is better. The main "is the number about right" metric — robust to a few very small/large SKUs skewing a plain average. |
| **MAE** / **RMSE** | Average forecast error in raw units. Useful for "how many units off" intuition, but not comparable across SKUs of different sizes (that's what WAPE is for). |
| **Bias** | Mean of (actual − predicted). Positive = the model under-forecasts on average; negative = it over-forecasts. |
| **Pinball / quantile loss** (`qloss_p10/p50/p90`) | How well-calibrated each quantile line is, not just P50 — penalizes a P90 line that's too low more than one that's too high, and vice versa for P10. Lower is better. |
| **Coverage (P10–P90)** | Of all actual outcomes, what % fell inside the model's predicted P10–P90 band. Target is 0.80 (an 80% interval should contain the truth 80% of the time) — too low means the band is overconfident, too high means it's wider than it needs to be. |
| **Surge recall** | Of the surges that actually happened, what % did the model flag as a surge (predicted ≥1.2× the SKU's baseline)? Higher is better — this is "did we miss it." |
| **Surge false-alarm rate** | Of the times demand *stayed* normal, what % did the model wrongly flag as a surge? Lower is better — this is "did we cry wolf." |
| **Surge precision** | Of every surge alarm the model raised, what % were real surges? Higher is better — this is "can the seller trust an alarm." |
| **Persistence AUC** | How well the trained persistence classifier ranks "will still be surging" vs. "won't" — 0.5 is a coin flip, 1.0 is perfect separation. |
| **Surge detection lead time** | Hours between the model's first alarm and the surge's actual onset (from `latent_truth.csv`, evaluator-only — never a training feature). Positive = advance warning; this is the number that turns into "you have 1–2 days to react" in the pitch. |

### Score table — seed 42 test split (`artifacts/test_comparison.csv`)

WAPE, coverage, and pinball loss for both baselines and both trained models, all scored on the exact same 1,092–1,405 held-out test rows per horizon:

| Horizon | Model | WAPE | Bias (units) | Pinball P50 | Coverage (P10–P90) |
| --- | --- | --- | --- | --- | --- |
| 24h | seasonal_naive | 0.433 | +13.4 | 26.60 | 0.744 |
| 24h | moving_average | 0.347 | +10.4 | 21.34 | 0.773 |
| 24h | transaction | 0.284 | +9.6 | 17.45 | 0.824 |
| 24h | **transaction_content (served)** | **0.298** | **+8.3** | **18.33** | **0.864** |
| 48h | seasonal_naive | 0.405 | +17.0 | 48.42 | 0.639 |
| 48h | moving_average | 0.331 | +19.3 | 39.62 | 0.740 |
| 48h | transaction | 0.299 | +22.6 | 35.81 | 0.810 |
| 48h | **transaction_content (served)** | **0.302** | **+19.9** | **36.07** | **0.801** |
| 72h | seasonal_naive | 0.386 | +26.4 | 68.71 | 0.605 |
| 72h | moving_average | 0.310 | +37.5 | 55.28 | 0.778 |
| 72h | transaction | 0.271 | +49.7 | 48.24 | 0.860 |
| 72h | **transaction_content (served)** | **0.275** | **+38.9** | **48.93** | **0.864** |

Both trained models clearly beat both baselines at every horizon; content
is roughly tied with transaction-only on this *one* seed (seed 42 happens
to be the most content-unfavourable of the eight tested — see below).

### Score table — 8-seed mean (`PROGRESS.md`, `scripts/multiseed_comparison.py`)

A single dataset seed isn't reliable evidence for whether the content
signal helps — it has flipped sign between seeds before. These are means
across 8 independently generated datasets (seeds 42, 7, 13, 101, 202, 303,
404, 505), comparing `transaction_content` against `transaction`-only:

| Metric (8-seed mean) | 24h | 48h | 72h |
| --- | --- | --- | --- |
| WAPE change from adding content | **−7.4%** | **−6.7%** | **−7.1%** |
| Surge persistence AUC (transaction → +content) | 0.723 → 0.820 | 0.614 → 0.704 | 0.601 → 0.649 |
| Surge recall | 0.569 | 0.569 | 0.611 |
| Surge false-alarm rate | 0.147 | 0.125 | 0.114 |
| Surge precision | 0.715 | 0.727 | 0.752 |
| Surge detection lead time (median) | +24.9h | +22.7h | +19.7h |
| Bias, % of actual demand | +8.8% | +10.6% | +12.9% |

Reading this table: content features cut point-error by 7% and meaningfully
improve the model's ability to tell "this surge will stick" from "this will
fade" (the AUC row) — especially at 24h, where it goes from a fairly weak
0.72 to a strong 0.82. Surge alarms are raised 1–2 days ahead of the actual
onset on average, with roughly 3 out of 4 alarms turning out to be real
(precision) and only 11–15% of quiet periods wrongly flagged. The main
known weakness is the bias row: the model runs 9–13% light on average,
which `PROGRESS.md` traces to the test split genuinely running hotter than
training (distribution shift), not to the model under-fitting — see
`PROGRESS.md` for the train/val/test bias breakdown that supports this.

## Decision engine (`apps/decisionengine/services.py`)

Deterministic, no ML or LLM required — it only needs a `ForecastOutput`
(dummy or trained) and a `DecisionConfig`. For a given SKU it:

1. Computes a unit ceiling from whichever binds: production capacity
   (`daily_capacity_minutes × horizon_days ÷ production_minutes_per_unit`)
   or working capital (`working_capital_limit ÷ unit_variable_cost`).
   Replenishment SKUs have no capacity term, so capital is the only ceiling.
2. Builds three purchase candidates against that ceiling — **COMMIT_NOW**
   (order to cover P90 demand immediately), **STAGED_COMMITMENT** (order to
   cover P50 now, with a second tranche at the decision midpoint that only
   ships if demand held up to at least P50 by then — otherwise it's treated
   as cancelled before it ships), **WAIT** (defer the full P90 order to the
   deadline) — plus a **NO_BUY_UNPROFITABLE** baseline every purchase has to
   beat. Quantities are rounded down to whole units and clamped to the
   `minimum_commitment` (MOQ): a batch below MOQ is rounded up if the
   ceiling allows, or dropped to 0 otherwise.
3. Scores every candidate across three demand scenarios — P10/P50/P90
   weighted 0.3/0.4/0.3 (Swanson's rule) — rather than P50 alone, so sizing
   for a P90 safety margin isn't scored as a guaranteed loss in the P50
   scenario it can never be sold in.
4. Per scenario, computes total business contribution: revenue from
   successfully delivered/non-returned/non-cancelled units, minus cost of
   goods, shipping-failure cost, residual-stock markdown (procurement cost
   minus salvage value, zeroed out if shelf life won't survive the
   horizon), and a holding cost on average inventory. Cancellation,
   delivery-failure, and return rates are measured from the SKU's own
   30-day order history (not assumed) via `_fulfilment_rates`; only the
   holding-cost rate is a stated assumption (0.1%/unit/day), since nothing
   in `DecisionConfig` carries it.
5. Picks the candidate with the highest expected contribution (ties broken
   toward less capital at risk, committed later); if nothing beats buying
   nothing, recommends `NO_BUY_UNPROFITABLE` and shows the three purchases
   as alternatives so the seller can see what each would have cost. If
   demand is already fully covered by stock, or capacity/capital/MOQ block
   every option, reports `NO_BUY_NEEDED`/`NO_BUY_POSSIBLE` instead and drops
   the (identical, zero-unit) alternatives.
6. `confidence` (`HIGH`/`MEDIUM`/`LOW`) reflects data quality:
   `insufficient_history` → LOW, no content signal → MEDIUM, otherwise HIGH.
7. Every economic assumption used (capacity, lead time, MOQ, shelf life,
   measured fulfilment rates, demand-scenario weights, holding-cost rate)
   is written out in `DecisionResult.assumptions` in plain Indonesian, so
   every number in the recommendation traces back to a stated input (PRD §19).

**Known simplification:** evaluates one SKU at a time — no cross-SKU
capacity allocation (PRD FR-O07, marked P1).

## Agent orchestration (`apps/agent/`)

`Agent.run(sku, now=None, overrides=None, explain=False)`:

1. `validate_data` — flags stale data (>3h old), missing history, or a
   stockout on the latest observation (censored demand warning).
2. Calls the forecast provider for horizons 24/48/72h and uses the 48h one
   to drive the decision engine.
3. Calls `DecisionEngine.evaluate_actions` with the current stock and any
   what-if `overrides` (capacity/capital/decision window, from the
   dashboard's what-if form).
4. Builds an `ExplanationPacket` (forecast + decision + warnings) — the
   *only* thing the LLM explainer is allowed to see, so it can't invent
   numbers.
5. If `explain=True`, calls the explainer and returns the narrative too.

### Explanation (`apps/agent/explainer.py`)

Three interchangeable `LLMExplainer` implementations, chosen by
`get_explainer()` from the environment (`.env`, see `.env.example`):

- **`GroqLLMExplainer`** (default) — Groq's free developer tier over an
  OpenAI-compatible endpoint. Default model `openai/gpt-oss-120b`: 30
  req/min, 1,000 req/day, 200k tokens/day, no credit card. Get a key at
  <https://console.groq.com/keys>. Set `GROQ_MODEL=llama-3.1-8b-instant` for
  14,400 req/day instead.
- **`GeminiLLMExplainer`** — Google Gemini flash-tier, kept as an
  alternative for deployments that already hold a Gemini key.
- **`DummyLLMExplainer`** — deterministic Markdown template, used whenever
  no key is set or a live call fails (network error, timeout, empty
  response, rate limit after retries). Every LLM explainer shares this as
  its fallback, so the explanation panel is never blank.

Select with `LLM_PROVIDER=groq|gemini|dummy` in `.env` (blank auto-picks
Groq when `GROQ_API_KEY` is set, else Gemini, else the template). The
prompt hands the model *only* the numbers already computed by the decision
engine and instructs it (in Bahasa Indonesia) to never invent a number and
never use analyst jargon ("commit", "fill rate", "SKU") — sellers read
"siapkan stok", "permintaan terpenuhi", "produk". To add another provider,
subclass `_HTTPLLMExplainer` and implement `_call_once`; prompt
construction, the retry loop (transient HTTP 429/500/502/503, network
errors), and the template fallback are all shared.

## Dashboard (`apps/dashboard/`)

Server-rendered Django templates + [HTMX](https://htmx.org) for partial
swaps + [Chart.js](https://www.chartjs.org) for the demand/forecast charts
(both vendored locally under `apps/dashboard/static/dashboard/js/`, with a
CDN `<script>` fallback if the local copy 404s). No SPA build step.

Every page requires login (`LoginRequiredMiddleware` in
`config/settings.py`); `django.contrib.auth.urls` provides
`/accounts/login/` etc. Every queryset is filtered by `owner=request.user`,
so two sellers logged in at once never see each other's SKUs.

| Route | View | What it does |
| --- | --- | --- |
| `/` | `overview` | Per-product summary cards (stock, surge persistence, recommended action, "needs attention" flag), draft count, recent approved drafts. |
| `/decisions/` , `/decisions/<sku_id>/` | `decision_center` | Runs the full agent pipeline for one SKU: forecast table, recommended action + alternatives, a what-if form (override capacity/capital/decision window without saving), recent action-plan drafts, inline Chart.js data. |
| `/products/` | `product_list` | All of the seller's SKUs with latest stock. |
| `/products/add/` , `/products/<sku_id>/edit/` | `product_create` / `product_edit` | `SKUForm` + `DecisionConfigForm`; picking a `constraint_profile` pre-fills defaults (editable) via `CONSTRAINT_PROFILE_PRESETS`, and the form only requires the fields relevant to the chosen `operation_mode` (capacity+production-minutes for `PRODUCTION`, lead time for `REPLENISHMENT`). |
| `/products/<sku_id>/` | `product_detail` | One product's latest snapshot + recent drafts. |
| `/plans/` | `plan_list` | All approved `ActionPlanDraft`s for the seller. |
| `/<sku_id>/approve/` (POST) | `approve_plan` | Re-runs the agent with the *exact* what-if overrides the seller reviewed, and persists that recommendation as a draft. Never places a real order. |
| `/api/chart-data/`, `/partials/dashboard/`, `/api/whatif/` | `api_views` | HTMX/JSON endpoints backing the what-if form and chart refresh — same `Agent().run()` pipeline, no separate business logic. |
| `/decisions/<sku_id>/explanation/` (POST) | `api_views.explanation_partial` | The only endpoint that calls the LLM (`explain=True`) — triggered by the "Analisis" button, not on page load, so rendering a page never silently spends LLM quota. |

## Known issues / MVP simplifications

- **`seed_dummy_data` is broken on a fresh database** — see Quickstart
  above. It predates per-seller login and was never updated to set
  `SKU.owner`. `seed_fashion_data` is the tested, currently-working seeder.
- `seed_dummy_data`'s synthetic generator is a simplified sine-wave
  heuristic, distinct from the reproducible event-seed simulator in
  `scripts/generate_dummy_data.py` (used for ML training/eval, not for
  seeding the web app's demo DB).
- The decision engine evaluates one SKU at a time — no cross-SKU capacity
  allocation (PRD FR-O07, P1).
- No real TikTok/marketplace integration — all data is synthetic; nothing
  in this MVP performs a real transaction or purchase order.
- Forecast quality caveats (72h bias under distribution shift, ~76%
  coverage on a nominal 80% interval on average across seeds) are tracked
  in [`PROGRESS.md`](PROGRESS.md) and [`data/README.md`](data/README.md),
  not repeated here.

## Project layout

```
apps/
  skus/            SKU, DecisionConfig, HourlyObservation models + seed commands
  forecasting/      ForecastProvider, features/quantile_model/scenarios, LightGBM provider
  decisionengine/   DecisionEngine, ActionPlanDraft model
  agent/            Agent orchestrator, LLMExplainer implementations
  dashboard/        Views, forms, templates, HTMX/API views, static assets
config/             Django settings/urls/wsgi/asgi
scripts/            Data generation + model training/evaluation CLIs (not part of the web app)
data/               Synthetic dataset produced by scripts/generate_dummy_data.py (see data/README.md)
artifacts/          Trained model bundles (gitignored; produced by scripts/train_forecast.py)
RamAI_PRD.md        Full product spec
design.md           Brand + UI design system
PROGRESS.md         ML track handoff notes: scores, open items, decisions/reversals, traps
```
