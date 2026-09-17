# VIRALCAST synthetic dataset — Synthetic Demo Data

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

Multi-seed (3 seeds) test-split results, transaction-only vs transaction-plus-content:

| Content lift | 24h | 48h | 72h |
| --- | --- | --- | --- |
| Point accuracy (WAPE) | −6.4% | −3.6% | +4.0% |
| Surge persistence (AUC) | +0.062 | +0.030 | −0.064 |

**Content features do not clearly help on this data.** The one consistent effect across
every version of the dataset is that content *hurts* short-horizon point accuracy: with
an 8–40 h conversion lag, a spike visible now has not converted inside 24 h, and the
model over-reacts to it.

An earlier run on a more heavily censored dataset showed a large persistence gain
(AUC +0.229 at 48 h). That did not survive fixing the censoring rate — it came from
1.2k-row test sets shrinking to 0.5k under selection. Treat any content advantage as
unproven until it holds on a larger test set.
