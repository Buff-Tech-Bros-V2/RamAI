"""Transaction vs transaction+content across several dataset seeds (PRD 16.3).

A single seed is not evidence. Differences between the two feature modes on one
synthetic dataset have been large enough to flip sign between seeds, so every
claim about whether the content block helps is made on the average across
seeds, with the per-seed spread shown so a reader can judge the noise.

Two questions are scored, because they are not the same question:

    point accuracy      WAPE / bias on cumulative demand  (FR-F02 vs FR-F03)
    surge persistence   AUC of "still above baseline at H" (FR-F05)

    python scripts/multiseed_comparison.py --seeds 42 7 13
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.forecasting import evaluation
from apps.forecasting.features import (  # noqa: E402
    HORIZONS, MODE_CONTENT, MODE_TRANSACTION, build_training_frame, load_observations,
)
from apps.forecasting.quantile_model import train_bundle  # noqa: E402

MODES = (MODE_TRANSACTION, MODE_CONTENT)
THRESHOLD = 1.2


def generate(seed: int, out: Path) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_dummy_data.py"),
         "--seed", str(seed), "--out", str(out)],
        check=True, capture_output=True, cwd=str(ROOT),
    )


def evaluate_seed(obs, labels, truth, seed: int) -> list[dict]:
    base_series = evaluation.baseline_hourly_series(obs)
    rows = []
    for mode in MODES:
        bundle = train_bundle(obs, labels, feature_mode=mode, verbose=False)
        for h in HORIZONS:
            X, y, meta = build_training_frame(obs, labels, horizon=h, feature_mode=mode)
            te = (meta["split"] == "test").to_numpy()
            X_te, y_te, meta_te = X[te], y[te], meta[te].reset_index(drop=True)
            actual = y_te.to_numpy()
            pred = bundle.predict(X_te, horizon=h)
            err = actual - pred["p50"].to_numpy()

            ref = evaluation.baseline_cumulative_reference(meta_te, base_series, h)
            is_surge = evaluation.label_surge_rows(meta_te, truth)
            s_metrics = evaluation.surge_detection_metrics(
                actual, pred["p50"], is_surge, alarm_ratio=THRESHOLD, reference=ref
            )
            lt_res = evaluation.surge_detection_lead_time(
                truth, meta_te, pred["p50"], ref, alarm_ratio=THRESHOLD
            )

            p_auc = float("nan")
            base_rate = float("nan")
            if bundle.has_persistence(h):
                meta_base = meta_te.merge(base_series, on=["timestamp", "sku_id"], how="left")
                rate = actual / h
                base_val = meta_base["baseline_hourly"].to_numpy(dtype=float)
                persist_label = (rate >= THRESHOLD * base_val).astype(int)
                ok = np.isfinite(base_val) & (base_val > 0)
                prob = bundle.predict_persistence(X_te, h).to_numpy()
                p_auc = float(roc_auc_score(persist_label[ok], prob[ok]))
                base_rate = float(persist_label[ok].mean())

            rows.append({
                "seed": seed, "mode": mode, "horizon": h, "n": int(te.sum()),
                "wape": float(np.abs(err).sum() / actual.sum()),
                "bias": float(err.mean()),
                "bias_pct": float(err.mean() / actual.mean() * 100),
                "coverage": float(np.mean((actual >= pred["p10"].to_numpy())
                                          & (actual <= pred["p90"].to_numpy()))),
                "auc": p_auc,
                "base_rate": base_rate,
                "surge_recall": s_metrics["recall"],
                "surge_false_alarm": s_metrics["false_alarm_rate"],
                "surge_precision": s_metrics["precision"],
                "surge_f1": s_metrics["f1"],
                "lead_time_median": lt_res["median_lead_time_hours"],
                "lead_time_mean": lt_res["mean_lead_time_hours"],
                "episodes_detected": lt_res["detected_episodes"],
                "episodes_total": lt_res["total_episodes"],
            })
    return rows


def lift_table(df: pd.DataFrame, metric: str, higher_is_better: bool) -> pd.DataFrame:
    wide = df.pivot_table(index=["seed", "horizon"], columns="mode", values=metric)
    delta = wide[MODE_CONTENT] - wide[MODE_TRANSACTION]
    if not higher_is_better:      # for WAPE, express as % improvement
        delta = delta / wide[MODE_TRANSACTION] * 100
    return delta.unstack("horizon")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 13])
    ap.add_argument("--out", default="artifacts/multiseed_comparison.csv")
    args = ap.parse_args()

    rows = []
    workdir = Path(tempfile.mkdtemp(prefix="viralcast_seeds_"))
    try:
        for seed in args.seeds:
            data = workdir / f"seed_{seed}"
            print(f"[seed {seed}] generating data ...", flush=True)
            generate(seed, data)
            obs = load_observations(data / "hourly_observations.parquet")
            labels = pd.read_parquet(data / "training_labels.parquet")
            truth = pd.read_csv(data / "latent_truth.csv", parse_dates=["timestamp"])
            truth["timestamp"] = pd.to_datetime(truth["timestamp"], utc=True).dt.tz_convert(
                obs["timestamp"].dt.tz
            )
            print(f"[seed {seed}] evaluating models ...", flush=True)
            rows += evaluate_seed(obs, labels, truth, seed)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    df = pd.DataFrame(rows)

    print("\n" + "=" * 78)
    print(f"POINT ACCURACY -- mean over {len(args.seeds)} seeds {args.seeds}")
    print("=" * 78)
    print(df.groupby(["horizon", "mode"])[["wape", "bias", "bias_pct", "coverage"]]
          .mean().round(3).to_string())

    print("\n" + "=" * 78)
    print("CONTENT LIFT (negative WAPE % = content better; positive AUC = content better)")
    print("=" * 78)
    w = lift_table(df, "wape", higher_is_better=False)
    a = lift_table(df, "auc", higher_is_better=True)
    print("\nWAPE change, % (per seed):")
    print(w.round(2).to_string())
    print("\n  mean:", {h: round(float(w[h].mean()), 2) for h in HORIZONS})
    print("\nPersistence AUC delta (per seed):")
    print(a.round(3).to_string())
    print("\n  mean:", {h: round(float(a[h].mean()), 3) for h in HORIZONS})

    print("\n" + "=" * 78)
    print("SURGE DETECTION METRICS (mean over seeds)")
    print("=" * 78)
    print(df.groupby(["horizon", "mode"])[["surge_recall", "surge_false_alarm", "surge_precision", "surge_f1", "lead_time_median"]]
          .mean().round(3).to_string())

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
