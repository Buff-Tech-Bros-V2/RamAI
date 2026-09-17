"""Train and evaluate the RamAI forecast models (PRD 16.3 experiment matrix).

Trains the seasonal-naive baseline, the transaction-only quantile model and the
transaction-plus-content quantile model on identical rows, then scores all three
on the same held-out test split.

    python scripts/train_forecast.py
    python scripts/train_forecast.py --data data --artifacts artifacts
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.forecasting import baseline, evaluation  # noqa: E402
from apps.forecasting.features import (  # noqa: E402
    HORIZONS, MODE_CONTENT, MODE_TRANSACTION, build_training_frame, load_observations,
)
from apps.forecasting.quantile_model import (  # noqa: E402
    feature_importance, train_bundle,
)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)


def evaluate_baseline(obs, labels, truth, horizon, model):
    frame = baseline.build_baseline_predictions(obs, labels, horizon, model=model)
    train = frame[frame["split"] == "train"]
    test = frame[frame["split"] == "test"].reset_index(drop=True)
    if test.empty:
        return None, None

    ratios = baseline.fit_residual_ratios(train["actual"], train["prediction"])
    q = baseline.baseline_quantiles(test["prediction"], ratios)

    row = evaluation.evaluate_quantiles(test["actual"], q["p10"], q["p50"], q["p90"])
    row["model"] = model
    row["horizon"] = horizon
    return row, (test, q)


def evaluate_model(bundle, obs, labels, horizon, feature_mode):
    X, y, meta = build_training_frame(obs, labels, horizon=horizon, feature_mode=feature_mode)
    is_test = (meta["split"] == "test").to_numpy()
    X_te, y_te, meta_te = X[is_test], y[is_test], meta[is_test].reset_index(drop=True)

    preds = bundle.predict(X_te, horizon=horizon)
    row = evaluation.evaluate_quantiles(y_te, preds["p10"], preds["p50"], preds["p90"])
    row["model"] = feature_mode
    row["horizon"] = horizon
    return row, (y_te.reset_index(drop=True), preds.reset_index(drop=True), meta_te)


def main() -> None:
    p = argparse.ArgumentParser(description="Train RamAI forecast models.")
    p.add_argument("--data", default="data")
    p.add_argument("--artifacts", default="artifacts")
    p.add_argument("--horizon-report", type=int, default=48,
                   help="horizon used for the detailed surge / episode breakdown")
    p.add_argument("--alarm-ratio", type=float, default=1.2,
                   help="ratio over baseline demand triggering a surge alarm")
    args = p.parse_args()

    data = Path(args.data)
    obs = load_observations(data / "hourly_observations.parquet")
    labels = pd.read_parquet(data / "training_labels.parquet")
    truth = pd.read_csv(data / "latent_truth.csv", parse_dates=["timestamp"])
    truth["timestamp"] = pd.to_datetime(truth["timestamp"], utc=True).dt.tz_convert(
        obs["timestamp"].dt.tz
    )

    print("=" * 78)
    print("TRAINING (train split fits, val split early-stops, test split untouched)")
    print("=" * 78)
    bundles = {}
    for mode in (MODE_TRANSACTION, MODE_CONTENT):
        bundles[mode] = train_bundle(obs, labels, feature_mode=mode)
        out = bundles[mode].save(Path(args.artifacts) / f"forecast_{mode}")
        print(f"  saved -> {out}\n")

    print("=" * 78)
    print("TEST-SET COMPARISON (PRD 16.3)")
    print("=" * 78)
    rows = []
    detail = {}
    for horizon in HORIZONS:
        for model in (baseline.MODEL_SEASONAL_NAIVE, baseline.MODEL_MOVING_AVERAGE):
            row, _ = evaluate_baseline(obs, labels, truth, horizon, model)
            if row:
                rows.append(row)
        for mode in (MODE_TRANSACTION, MODE_CONTENT):
            row, det = evaluate_model(bundles[mode], obs, labels, horizon, mode)
            rows.append(row)
            detail[(mode, horizon)] = det

    report = pd.DataFrame(rows)[
        ["horizon", "model", "n", "mae", "wape", "bias",
         "qloss_p10", "qloss_p50", "qloss_p90", "coverage_p10_p90", "interval_width"]
    ].round(3)
    print(report.to_string(index=False))

    h = args.horizon_report
    base_series = evaluation.baseline_hourly_series(obs)
    print("\n" + "=" * 78)
    print(f"SURGE BEHAVIOUR AT {h}h  (does content help without chasing false spikes?)")
    print(f"Alarm rule: P50 >= {args.alarm_ratio:.2f}x per-SKU trailing baseline")
    print("=" * 78)
    for mode in (MODE_TRANSACTION, MODE_CONTENT):
        y_te, preds, meta_te = detail[(mode, h)]
        ref = evaluation.baseline_cumulative_reference(meta_te, base_series, h)
        is_surge = evaluation.label_surge_rows(meta_te, truth)
        surge = evaluation.surge_detection_metrics(
            y_te, preds["p50"], is_surge, alarm_ratio=args.alarm_ratio, reference=ref
        )
        print(f"\n{mode}:  recall={surge['recall']:.3f}  "
              f"false_alarm={surge['false_alarm_rate']:.3f}  precision={surge['precision']:.3f}  "
              f"f1={surge['f1']:.3f}  (alarms={surge['alarm_count']})")
        by_ep = evaluation.metrics_by_episode_type(meta_te, truth, y_te, preds["p50"])
        print(by_ep.round(2).to_string(index=False))

    print("\n" + "=" * 78)
    print("SURGE DETECTION LEAD TIME (PRD 16.1)")
    print("=" * 78)
    for horizon in HORIZONS:
        print(f"\nHorizon {horizon}h:")
        for mode in (MODE_TRANSACTION, MODE_CONTENT):
            y_te, preds, meta_te = detail[(mode, horizon)]
            ref = evaluation.baseline_cumulative_reference(meta_te, base_series, horizon)
            lt_res = evaluation.surge_detection_lead_time(
                truth, meta_te, preds["p50"], ref, alarm_ratio=args.alarm_ratio
            )
            print(f"  {mode:20s}  detected={lt_res['detected_episodes']}/{lt_res['total_episodes']}  "
                  f"median_lead_time={lt_res['median_lead_time_hours']:+.1f}h  "
                  f"mean_lead_time={lt_res['mean_lead_time_hours']:+.1f}h")

    print("\n" + "=" * 78)
    print("SURGE PERSISTENCE (FR-F05) ON TEST SPLIT")
    print("=" * 78)
    from sklearn.metrics import brier_score_loss, roc_auc_score
    for horizon in HORIZONS:
        print(f"\nHorizon {horizon}h:")
        for mode in (MODE_TRANSACTION, MODE_CONTENT):
            if bundles[mode].has_persistence(horizon):
                X, y, meta = build_training_frame(obs, labels, horizon=horizon, feature_mode=mode)
                te = (meta["split"] == "test").to_numpy()
                X_te, y_te, meta_te = X[te], y[te], meta[te].reset_index(drop=True)
                meta_base = meta_te.merge(base_series, on=["timestamp", "sku_id"], how="left")
                rate = y_te.to_numpy(dtype=float) / horizon
                base_val = meta_base["baseline_hourly"].to_numpy(dtype=float)
                persist_label = (rate >= 1.2 * base_val).astype(int)
                ok = np.isfinite(base_val) & (base_val > 0)
                prob = bundles[mode].predict_persistence(X_te, horizon).to_numpy()
                auc = float(roc_auc_score(persist_label[ok], prob[ok]))
                brier = float(brier_score_loss(persist_label[ok], prob[ok]))
                print(f"  {mode:20s}  AUC={auc:.4f}  Brier={brier:.4f}")

    print("\n" + "=" * 78)
    print(f"TOP FEATURES ({MODE_CONTENT}, {h}h, P50)")
    print("=" * 78)
    print(feature_importance(bundles[MODE_CONTENT], h).round(4).to_string(index=False))

    art = Path(args.artifacts)
    art.mkdir(parents=True, exist_ok=True)
    report.to_csv(art / "test_comparison.csv", index=False)
    (art / "training_summary.json").write_text(json.dumps({
        "horizons": list(HORIZONS),
        "modes": [MODE_TRANSACTION, MODE_CONTENT],
        "test_rows": int(report.loc[report["model"] == MODE_CONTENT, "n"].iloc[0]),
        "alarm_ratio": args.alarm_ratio,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote {art / 'test_comparison.csv'}")


if __name__ == "__main__":
    main()
