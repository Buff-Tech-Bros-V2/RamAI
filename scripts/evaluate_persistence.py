"""Does the content signal predict surge PERSISTENCE? (PRD FR-F05)

Point accuracy asks "how many units". The decision engine asks a different
question: "will demand still be above baseline when my extra stock lands?"
This script scores that question directly.

Label: demand over the next H hours averaged at least `threshold` x the
pre-cutoff baseline hourly level. Built from observed fulfillment demand on
uncensored windows -- never from latent truth, which stays evaluator-only.

    python scripts/evaluate_persistence.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.forecasting.evaluation import baseline_hourly_series  # noqa: E402
from apps.forecasting.features import (  # noqa: E402
    MODE_CONTENT, MODE_TRANSACTION, build_training_frame, load_observations,
)

PARAMS = {
    "objective": "binary", "metric": "auc", "learning_rate": 0.05,
    "num_leaves": 31, "min_data_in_leaf": 40, "feature_fraction": 0.85,
    "bagging_fraction": 0.85, "bagging_freq": 1, "lambda_l2": 1.0,
    "verbosity": -1, "seed": 42,
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="data")
    p.add_argument("--horizon", type=int, default=48)
    p.add_argument("--threshold", type=float, default=1.2)
    args = p.parse_args()

    data = Path(args.data)
    obs = load_observations(data / "hourly_observations.parquet")
    labels = pd.read_parquet(data / "training_labels.parquet")
    base = baseline_hourly_series(obs)

    horizon = args.horizon
    results = {}
    for mode in (MODE_TRANSACTION, MODE_CONTENT):
        X, y, meta = build_training_frame(obs, labels, horizon=horizon, feature_mode=mode)
        meta = meta.merge(base, on=["timestamp", "sku_id"], how="left")

        rate = y.to_numpy() / horizon
        persist = (rate >= args.threshold * meta["baseline_hourly"].to_numpy()).astype(int)
        ok = np.isfinite(meta["baseline_hourly"].to_numpy()) & (meta["baseline_hourly"] > 0).to_numpy()

        X, persist, meta = X[ok], persist[ok], meta[ok].reset_index(drop=True)
        tr = (meta["split"] == "train").to_numpy()
        va = (meta["split"] == "val").to_numpy()
        te = (meta["split"] == "test").to_numpy()

        booster = lgb.train(
            PARAMS, lgb.Dataset(X[tr], label=persist[tr]), num_boost_round=600,
            valid_sets=[lgb.Dataset(X[va], label=persist[va])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        prob = booster.predict(X[te])
        results[mode] = {
            "auc": roc_auc_score(persist[te], prob),
            "brier": brier_score_loss(persist[te], prob),
            "base_rate": float(persist[te].mean()),
            "n_test": int(te.sum()),
            "booster": booster,
            "prob": prob,
            "y": persist[te],
        }

    print("=" * 74)
    print(f"SURGE PERSISTENCE AT {horizon}h  (demand >= {args.threshold}x baseline)")
    print("=" * 74)
    r0, r1 = results[MODE_TRANSACTION], results[MODE_CONTENT]
    print(f"test rows={r0['n_test']}  positive rate={r0['base_rate']:.3f}\n")
    for mode, r in results.items():
        print(f"  {mode:20s}  AUC={r['auc']:.4f}  Brier={r['brier']:.4f}")
    lift = (r1["auc"] - r0["auc"]) / r0["auc"] * 100
    print(f"\n  content lift: AUC {r1['auc'] - r0['auc']:+.4f} ({lift:+.2f}%)  "
          f"Brier {r1['brier'] - r0['brier']:+.4f}")

    imp = pd.DataFrame({
        "feature": r1["booster"].feature_name(),
        "gain": r1["booster"].feature_importance("gain"),
    })
    imp["share"] = imp["gain"] / imp["gain"].sum()
    content_share = imp[imp["feature"].str.contains("content|affiliate|views")]["share"].sum()
    print(f"\n  content features carry {content_share * 100:.1f}% of total gain")
    print("\nTop 12 features (content model):")
    print(imp.sort_values("gain", ascending=False).head(12).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
