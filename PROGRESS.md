# VIRALCAST — ML / forecasting track progress

Handoff note for the next session. Branch `feat/ml-model`, 15 commits ahead of `main`.

## Scope and standing constraints

- **This track owns forecasting only.** `apps/forecasting/` and `scripts/`. Backend,
  decision engine, agent and dashboard belong to teammates — do not modify them.
  `apps/forecasting/services.py` is the one shared file touched, and only the
  `FORECAST_FEATURE_MODE` constant and the `get_forecast_provider()` factory.
- **No TDD, no tests** unless explicitly asked. Tests written earlier were removed
  at the user's request (commit `d4c6ebc`).
- **Conventional Commits**, imperative mood, never add a co-author trailer.
- **`data/latent_truth.csv` is evaluator-only.** It may be used to label episodes when
  scoring, never as a model feature.

## Where things stand

The pipeline runs end to end: synthetic data → features → quantile models → scenarios →
a `ForecastProvider` the rest of the app can call without changing.

| File | Role |
| --- | --- |
| `scripts/generate_dummy_data.py` | Synthetic generator, PRD 10.2 order. Reproducible from `--seed`. |
| `apps/forecasting/features.py` | Feature pipeline. 48 cols transaction-only, 73 with content. Leakage-guarded. |
| `apps/forecasting/quantile_model.py` | LightGBM quantile bundles. **Ratio target + conformal intervals.** |
| `apps/forecasting/baseline.py` | seasonal_naive and moving_average comparators. |
| `apps/forecasting/evaluation.py` | PRD 16.1 metrics. **Lead time is missing — see open items.** |
| `apps/forecasting/scenarios.py` | Block-bootstrap scenarios, surge persistence (FR-F05). |
| `apps/forecasting/provider.py` | `LightGBMForecastProvider` implementing the PRD 12 contract. |
| `scripts/train_forecast.py` | Trains both modes, writes `artifacts/`, prints the 16.3 table. |
| `scripts/evaluate_persistence.py` | Direct persistence classifier (the sound one). |
| `scripts/multiseed_comparison.py` | 8-seed transaction vs content comparison. |

### Reproduce

```bash
python scripts/generate_dummy_data.py --seed 42
python scripts/train_forecast.py                       # writes artifacts/
python scripts/multiseed_comparison.py --seeds 42 7 13 101 202 303 404 505
```

`artifacts/` is gitignored — retrain before anything that needs a bundle. Without
artifacts the factory falls back to `DummyForecastProvider`, so the app still boots.

## Scores and the bar we want to hit

Served model is `transaction_content` (`services.py:31`). Seed 42 test split, with the
8-seed mean where available.

| Parameter | 24h | 48h | 72h | Proposed bar | Status |
| --- | --- | --- | --- | --- | --- |
| WAPE (8-seed) | 0.261 | 0.285 | 0.294 | <= 0.30 and >=10% better than moving_average | PASS (48h margin only +10%) |
| MAE (seed 42) | 35.7 | 71.3 | 97.5 | no absolute bar; track direction | — |
| Pinball p50 (seed 42) | 17.84 | 35.63 | 48.73 | beat moving_average at p10/p50/p90 | PASS, all nine |
| Coverage, nominal 0.80 (8-seed) | 0.766 | 0.755 | 0.770 | 0.75–0.85 | MARGINAL, bottom edge |
| Bias, % of actual (8-seed) | +8.5% | +10.5% | +13.4% | abs(bias) <= 10% | 24h pass, 48h borderline, **72h fail** |
| Persistence AUC (8-seed) | 0.820 | 0.704 | 0.649 | >= 0.70 | 24h pass, 48h marginal, **72h fail** |
| Surge recall (seed 42) | 0.463 | 0.411 | 0.332 | >= 0.60 | **FAIL** |
| Surge false-alarm (seed 42) | 0.321 | 0.343 | 0.341 | <= 0.20 | **FAIL** |
| Surge detection lead time | — | — | — | >= 6h median | **NOT IMPLEMENTED** |

Baselines, seed 42 WAPE: moving_average 0.347 / 0.331 / 0.310, seasonal_naive
0.433 / 0.405 / 0.386. The models beat both at every horizon.

**The thresholds above are a proposal, not the PRD's.** PRD 16.1 lists which metrics to
compute and sets no numeric targets; 19's acceptance criteria are structural pass/fail.
Agree these with the team before quoting them as targets.

PRD 19 criteria belonging to this track — all currently pass: P10/P50/P90 available for
every SKU; baseline and enriched evaluated on the same test set; model runs without
content features; stockout periods not treated as zero demand; forecast contract
unchanged when operation mode switches.

## Open items, highest value first

1. **Tune the surge alarm rule before blaming the model.** `evaluation.surge_detection_metrics`
   fires when predicted >= `1.3 * median(actual)` — a crude global trigger. A per-SKU
   baseline and a swept ratio would likely move recall and false-alarm together. This is
   the demo's headline metric and currently its weakest. ~30 min.
2. **Implement surge detection lead time (PRD 16.1).** Nothing measures it anywhere.
   Definition to agree: hours between the first alarm and the episode's true onset in
   `latent_truth.csv`. ~1 hour.
3. **Replace the provider's persistence proxy.** `provider._persistence` feeds
   `residual_blocks` synthetic uniform noise derived from the P10/P90 ratio, because the
   bundle stores no real residuals — so persistence is nearly a deterministic function of
   P50/baseline. The sound implementation is the trained classifier in
   `evaluate_persistence.py`. It does discriminate correctly now (0.0 quiet vs 0.72–1.0 in
   surge) after the intervals were calibrated, so this is correctness, not a visible bug.
4. **72h is the weakest horizon** on both bias (+13.4%) and persistence (AUC 0.649).
   I would state this as a known limit rather than tune against the test split.

## Decisions and reversals worth knowing

- **Content features help — this reversed an earlier conclusion.** An earlier note said
  content did not help and hurt 24h accuracy by -6.4%. That was a correct measurement of a
  *worse model* (absolute target). Under the ratio target, content wins on both questions
  across 8 seeds: WAPE -7.4% / -6.7% / -7.1%, persistence AUC +0.097 / +0.091 / +0.048.
  Mechanism: on an absolute target the model had to turn a content spike into a unit count
  and overshot; on the ratio scale the signal says "running 3x normal", which is the scale
  a leading indicator lives on.
- **Seed 42 — the dataset committed in `data/` — is the most content-unfavourable of the
  eight.** On seed 42 alone content looks neutral-to-harmful. Never quote seed 42 alone as
  evidence about content; quote the 8-seed mean.
- **Ratio target** (`quantile_model.py:81`) is why bias halved. Trees cannot extrapolate
  past the leaf averages seen in training, so an absolute target could not express "3x
  normal" for a SKU whose normal level it had never seen that high.
- **Conformal intervals** (`quantile_model.py:233`) fitted on validation only. Raw boosters
  covered 72-76% of a nominal 80% band.
- **Residual bias is distribution shift, not model error.** On seed 42 the model is near
  unbiased on train (+4.1%) and val (+5.2%) and runs light only on test (+9.2%), because
  the test window genuinely runs hotter — raw hourly demand 5.10 vs 3.63 in train. A
  correction fitted on validation would not help; one fitted on test would be cheating.
- **Stock features were removed deliberately** (`features.py:127`). `stock_on_hand`
  proxied the replenishment rule the generator implements rather than demand, took 30% of
  gain, and removing it improved WAPE for both modes.
- **Censored rows are lower-demand, not higher** (mean 60 vs 92 at 24h), so dropping them
  does not truncate the top of the distribution. Censoring handling is correct as-is.

## Traps

- A single seed has twice pointed the opposite way on the content question. Always
  multi-seed before reporting a comparison.
- Mean vs median has caused the same bug twice (persistence labels, then
  `scenarios.baseline_hourly_level`). Scenario persistence compares a *mean* hourly rate,
  so the baseline must also be a mean.
- `python` alone may not resolve the venv — use `.venv/Scripts/python.exe` on this machine.
