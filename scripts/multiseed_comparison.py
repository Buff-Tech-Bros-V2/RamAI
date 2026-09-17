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

from apps.forecasting.features import (  # noqa: E402
    MODE_CONTENT, MODE_TRANSACTION, build_training_frame, load_observations,
)
from apps.forecasting.quantile_model import train_bundle  # noqa: E402
from scripts.evaluate_persistence import PARAMS, baseline_hourly_series  # noqa: E402

HORIZONS = (24, 48, 72)
MODES = (MODE_TRANSACTION, MODE_CONTENT)
THRESHOLD = 1.2


def generate(seed: int, out: Path) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_dummy_data.py"),
         "--seed", str(seed), "--out", str(out)],
        check=True, capture_output=True, cwd=str(ROOT),
    )


def point_accuracy(obs, labels, seed: int) -> list[dict]:
    rows = []
    for mode in MODES:
        bundle = train_bundle(obs, labels, feature_mode=mode, verbose=False)
        for h in HORIZONS:
            X, y, meta = build_training_frame(obs, labels, horizon=h, feature_mode=mode)
            te = (meta["split"] == "test").to_numpy()
            actual = y.to_numpy()[te]
            pred = bundle.predict(X[te], horizon=h)
            err = actual - pred["p50"].to_numpy()
            rows.append({
                "seed": seed, "mode": mode, "horizon": h, "n": int(te.sum()),
                "wape": float(np.abs(err).sum() / actual.sum()),
                "bias": float(err.mean()),
                "bias_pct": float(err.mean() / actual.mean() * 100),
                "coverage": float(np.mean((actual >= pred["p10"].to_numpy())
                                          & (actual <= pred["p90"].to_numpy()))),
            })
    return rows


def persistence(obs, labels, seed: int) -> list[dict]:
    base = baseline_hourly_series(obs)
    rows = []
    for mode in MODES:
        for h in HORIZONS:
            X, y, meta = build_training_frame(obs, labels, horizon=h, feature_mode=mode)
            meta = meta.merge(base, on=["timestamp", "sku_id"], how="left")
            rate = y.to_numpy() / h
            label = (rate >= THRESHOLD * meta["baseline_hourly"].to_numpy()).astype(int)
            ok = (np.isfinite(meta["baseline_hourly"].to_numpy())
                  & (meta["baseline_hourly"] > 0).to_numpy())
            X, label, meta = X[ok], label[ok], meta[ok].reset_index(drop=True)
            tr = (meta["split"] == "train").to_numpy()
            va = (meta["split"] == "val").to_numpy()
            te = (meta["split"] == "test").to_numpy()
            booster = lgb.train(
                PARAMS, lgb.Dataset(X[tr], label=label[tr]), num_boost_round=600,
                valid_sets=[lgb.Dataset(X[va], label=label[va])],
                callbacks=[lgb.early_stopping(50, verbose=False)],
            )
            rows.append({
                "seed": seed, "mode": mode, "horizon": h,
                "auc": float(roc_auc_score(label[te], booster.predict(X[te]))),
                "base_rate": float(label[te].mean()),
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

    acc_rows, per_rows = [], []
    workdir = Path(tempfile.mkdtemp(prefix="viralcast_seeds_"))
    try:
        for seed in args.seeds:
            data = workdir / f"seed_{seed}"
            print(f"[seed {seed}] generating ...", flush=True)
            generate(seed, data)
            obs = load_observations(data / "hourly_observations.parquet")
            labels = pd.read_parquet(data / "training_labels.parquet")
            print(f"[seed {seed}] point accuracy ...", flush=True)
            acc_rows += point_accuracy(obs, labels, seed)
            print(f"[seed {seed}] persistence ...", flush=True)
            per_rows += persistence(obs, labels, seed)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    acc = pd.DataFrame(acc_rows)
    per = pd.DataFrame(per_rows)

    print("\n" + "=" * 78)
    print(f"POINT ACCURACY -- mean over {len(args.seeds)} seeds {args.seeds}")
    print("=" * 78)
    print(acc.groupby(["horizon", "mode"])[["wape", "bias", "bias_pct", "coverage"]]
          .mean().round(3).to_string())

    print("\n" + "=" * 78)
    print("CONTENT LIFT (negative WAPE % = content better; positive AUC = content better)")
    print("=" * 78)
    w = lift_table(acc, "wape", higher_is_better=False)
    a = lift_table(per, "auc", higher_is_better=True)
    print("\nWAPE change, % (per seed):")
    print(w.round(2).to_string())
    print("\n  mean:", {h: round(float(w[h].mean()), 2) for h in HORIZONS})
    print("\nPersistence AUC delta (per seed):")
    print(a.round(3).to_string())
    print("\n  mean:", {h: round(float(a[h].mean()), 3) for h in HORIZONS})

    print("\nPersistence AUC by mode:")
    print(per.groupby(["horizon", "mode"])[["auc", "base_rate"]].mean().round(3).to_string())

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    acc.merge(per, on=["seed", "mode", "horizon"], how="outer").to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
