# RamAI synthetic dataset — Synthetic Demo Data

Regenerate with:

```bash
python scripts/generate_dummy_data.py --seed 42          # default: 5 SKUs, 90 days, ./data
python scripts/generate_dummy_data.py --seed 7 --out data_holdout   # unseen event seeds
```

Everything is reproducible from `--seed`. Same seed → byte-identical files.

## Files

| File | Rows | Use |
| --- | --- | --- |
| `hourly_observations.{csv,parquet}` | 10,800 (5 SKU × 2,160 h) | PRD 9.1 observable schema. **The only source of features.** |
| `training_labels.{csv,parquet}` | 10,800 | Targets: cumulative fulfillment demand 24/48/72h + censoring flags |
| `latent_truth.csv` | 10,800 | **Evaluator only.** `latent_demand`, episode labels. Never a feature. |
| `events.csv` | ~27 | One row per social-commerce episode with its `event_seed` |
| `decision_config.csv` | 5 | PRD 9.2 decision configuration (costs, capacity, capital, deadline) |
| `sku_master.csv` | 5 | Static SKU attributes and ground-truth process rates |
| `splits.json` | — | Time boundaries + event seeds per split |
| `metadata.json` | — | Seed, version, row counts, stockout/missing-content shares |

## Targets

`fulfillment_demand(t) = orders_created(t) - orders_cancelled_pre_ship(t)`

`target_demand_{24,48,72}h(t)` = sum of `fulfillment_demand` over the **next** H hours
(strictly after the cutoff `t`, so there is no same-row leakage).

`target_censored_{H}h(t) = 1` when any hour in that window had `stockout_flag = 1`.
**Drop or downweight those rows when training** — otherwise the model learns
"empty shelf ⇒ zero demand". Roughly 74–85% of labels are uncensored (24h: 84.6%,
48h: 77.7%, 72h: 73.6%), leaving 1,092–1,405 usable test rows per horizon.

The last H hours of each series have a null target by construction.

## Splits (FR-D05)

Time-based, 70 / 15 / 15. Episodes are planned per split window and each carries a
unique `event_seed`; the generator raises `AssertionError` if a seed ever spans two
splits. Every split contains all five episode archetypes, so the test set exercises
fading surges, false content spikes and missing-signal fallback too.

## Episode archetypes (FR-D04)

| Type | What it looks like | Why it exists |
| --- | --- | --- |
| `TRUE_SURGE` | content spike → demand holds ~2–3 days | persistence should be high |
| `FADING_SURGE` | sharp spike, half-life 5–10 h | staged commitment, not full commit |
| `FALSE_CONTENT` | big content velocity, ~2–10% conversion | model must not chase views |
| `MISSING_SIGNAL` | real demand surge, content columns all null | transaction-only fallback (FR-D06) |
| `SLOW_BURN` | modest, long-lived uplift | baseline drift, not a spike |
| `EP-CONFLICT-*` | SKU-001 + SKU-002 surge together in the test window | capacity conflict demo (Scenario C) |

## Content columns are nullable

`product_views`, `affiliate_orders`, `active_affiliates`, `content_view_velocity`
are `NaN` for ~10–19% of rows (missing-signal episodes plus random blackout blocks).
Feature code must handle all-null content and set a `content_features_used` flag.

## Signal structure worth knowing before modelling

- Content velocity **leads** orders by `conversion_lag_hours` (8–40 h) — that lead is the
  early-warning signal the enriched model is supposed to exploit.

  **This is a stated assumption, not a measurement.** It says a viewer saves or shares
  a video and buys later that day or the next, and that affiliate reach compounds after
  the original post. An earlier version used 1–8 h, which made the content signal
  worthless *by construction*: against 24–72 h forecast horizons, a 2-hour head start
  carries nothing the transaction series does not already contain by the cutoff. The
  value must be shown in the UI as an assumption and checked against real seller data
  in the PRD §21 Phase 1 pilot. Results are sensitive to it — see below.
- Demand is negative-binomial (overdispersed), not Poisson.
- Hour-of-day and day-of-week seasonality are strong; weekends peak.
- Prices drop 8–25% during promo windows; elasticity varies by SKU.
- Replenishment is reactive on *past observed* sales with a thin buffer, so surges
  genuinely outrun stock — that's where the censoring comes from.

## What the models found on this data

Reproduce with `python scripts/multiseed_comparison.py --seeds 42 7 13 101 202 303 404 505`.
Everything below is the mean over those **8 dataset seeds**, not one run -- the per-seed
spread is wide enough that a single seed has twice pointed the opposite way.

### Content features help, on both questions

| Content lift (8-seed mean) | 24h | 48h | 72h |
| --- | --- | --- | --- |
| Point accuracy (WAPE change) | **-7.4%** | **-6.7%** | **-7.1%** |
| Surge persistence (AUC delta) | **+0.097** | **+0.091** | +0.048 |

Persistence AUC by mode: 24h 0.723 -> 0.820, 48h 0.614 -> 0.704, 72h 0.601 -> 0.649.
WAPE favours content in 7 of 8 seeds at 24h and 48h, 6 of 8 at 72h. The 72h persistence
delta is the weakest of the six numbers -- treat 72h persistence as unproven.

**Seed 42 -- the dataset committed in this folder -- is the single most
content-unfavourable of the eight.** On seed 42 alone content looks neutral-to-harmful
(+4.3% WAPE at 24h). Do not quote seed 42 on its own as evidence either way.

### This reverses an earlier finding, because the model changed

An earlier version of this file reported that content did **not** help and actively hurt
24h accuracy (-6.4%). That was measured against a model trained on the **absolute**
demand count. It was a real measurement of a worse model, not noise.

The quantile models now fit a **ratio target** -- demand divided by the trailing level --
and that is what changed the answer. On an absolute target the model had to turn a content
spike into a unit count and consistently overshot; on the ratio scale the same signal says
"this is running 3x normal", which is the scale a leading indicator naturally lives on.
The mechanism and the reversal are both worth stating out loud rather than quietly
presenting the new number.

### Point accuracy against the baselines

Single-seed (42) test split, served content model vs baselines -- `artifacts/test_comparison.csv`:

| WAPE | 24h | 48h | 72h |
| --- | --- | --- | --- |
| seasonal_naive | 0.433 | 0.405 | 0.386 |
| moving_average | 0.347 | 0.331 | 0.310 |
| transaction | 0.278 | 0.298 | 0.273 |
| transaction_content | 0.290 | 0.298 | 0.274 |

The models beat both baselines at every horizon, including 72h where an earlier
absolute-target model lost to a plain moving average.

### Under-forecast bias is reduced, not eliminated

Bias is `mean(actual - predicted)`, so positive means under-forecasting. The ratio target
cut it roughly in half (8-seed mean, served content model): **+8.5% / +10.5% / +13.4%**
at 24/48/72h, from +18% / +25% / +27% under the absolute target.

What remains is not model error. On seed 42 the model is close to unbiased on the data it
was fitted and validated on (+4.1% train, +5.2% val at 24h) and only runs light on test
(+9.2%), because the test window genuinely runs hotter than the training period -- raw
hourly demand 5.10 there against 3.63 in train, which is where the forced conflict surges
sit. A multiplicative correction fitted on validation would not help, since validation is
already unbiased; one fitted on test would be cheating. **Expect this model to run ~10%
light during an unusually hot period.**

### Intervals are conformalised

Raw quantile boosters covered only 72-76% inside a nominal 80% P10-P90 band, which would
have left the decision engine short on safety stock. Each bundle stores a split-conformal
width correction per horizon, fitted on validation only. Seed 42 test coverage is
0.82 / 0.81 / 0.87; the 8-seed mean is ~0.76, so the band is still slightly optimistic
on average.
