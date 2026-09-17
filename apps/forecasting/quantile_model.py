"""LightGBM quantile forecast models (PRD FR-F02/F03/F04, 10.3).

One booster per (horizon, quantile), pooled across SKUs -- SKU identity is a
feature, not a reason to fit a separate small model. Two variants share this
code path and differ only in ``feature_mode``:

    transaction          FR-F02, transaction signals only
    transaction_content  FR-F03, plus the affiliate/content block

Target is cumulative fulfillment demand over the horizon, trained only on
label windows that were not stockout-censored (see features.build_training_frame).

    bundle = train_bundle(obs, labels, feature_mode=MODE_CONTENT)
    bundle.save("artifacts/forecast_content")
    preds  = bundle.predict(X, horizon=48)     # -> DataFrame p10/p50/p90
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .features import (
    HORIZONS,
    MODE_CONTENT,
    MODE_TRANSACTION,
    FEATURE_VERSION,
    build_training_frame,
    feature_columns,
)

MODEL_NAME = "lightgbm_quantile"
MODEL_VERSION = "forecast-v1"
QUANTILES = (0.10, 0.50, 0.90)

DEFAULT_PARAMS = {
    "objective": "quantile",
    "metric": "quantile",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 40,
    "feature_fraction": 0.85,
    "bagging_fraction": 0.85,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "num_threads": 0,
    "seed": 42,
}

NUM_BOOST_ROUND = 600
EARLY_STOPPING_ROUNDS = 50


@dataclass
class QuantileBundle:
    """All boosters for one feature mode: {(horizon, quantile): Booster}."""

    feature_mode: str
    boosters: dict = field(default_factory=dict)
    features: list[str] = field(default_factory=list)
    train_rows: dict = field(default_factory=dict)
    best_iterations: dict = field(default_factory=dict)

    model_name: str = MODEL_NAME
    model_version: str = MODEL_VERSION
    feature_version: str = FEATURE_VERSION

    # ------------------------------------------------------------ predict --
    def predict(self, X: pd.DataFrame, horizon: int) -> pd.DataFrame:
        """Return a p10/p50/p90 frame, monotonically sorted across quantiles."""
        if horizon not in HORIZONS:
            raise ValueError(f"unknown horizon {horizon}; expected one of {HORIZONS}")

        X = X[self.features]
        out = {}
        for q in QUANTILES:
            booster = self.boosters[(horizon, q)]
            pred = booster.predict(X, num_iteration=booster.best_iteration or None)
            out[f"p{int(q * 100)}"] = np.maximum(np.asarray(pred, dtype=float), 0.0)

        frame = pd.DataFrame(out, index=X.index)
        # quantile crossing is possible when boosters are fit independently;
        # sorting is the standard cheap fix and keeps p10 <= p50 <= p90.
        sorted_vals = np.sort(frame.to_numpy(), axis=1)
        return pd.DataFrame(sorted_vals, columns=["p10", "p50", "p90"], index=X.index)

    # --------------------------------------------------------------- io ----
    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for (horizon, q), booster in self.boosters.items():
            booster.save_model(str(directory / f"h{horizon}_q{int(q * 100)}.txt"))
        meta = {
            "feature_mode": self.feature_mode,
            "features": self.features,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "horizons": list(HORIZONS),
            "quantiles": list(QUANTILES),
            "train_rows": {str(k): v for k, v in self.train_rows.items()},
            "best_iterations": {f"{h}_{int(q * 100)}": v
                                for (h, q), v in self.best_iterations.items()},
        }
        (directory / "bundle.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return directory

    @classmethod
    def load(cls, directory: str | Path) -> "QuantileBundle":
        directory = Path(directory)
        meta = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
        boosters = {}
        for horizon in meta["horizons"]:
            for q in meta["quantiles"]:
                path = directory / f"h{horizon}_q{int(q * 100)}.txt"
                boosters[(horizon, q)] = lgb.Booster(model_file=str(path))
        return cls(
            feature_mode=meta["feature_mode"],
            boosters=boosters,
            features=meta["features"],
            train_rows={k: v for k, v in meta.get("train_rows", {}).items()},
            model_name=meta.get("model_name", MODEL_NAME),
            model_version=meta.get("model_version", MODEL_VERSION),
            feature_version=meta.get("feature_version", FEATURE_VERSION),
        )


# ------------------------------------------------------------------ train --

def train_bundle(obs: pd.DataFrame, labels: pd.DataFrame,
                 feature_mode: str = MODE_CONTENT,
                 horizons=HORIZONS,
                 params: dict | None = None,
                 verbose: bool = True) -> QuantileBundle:
    """Fit every (horizon, quantile) booster on the train split.

    The val split drives early stopping; the test split is never touched here.
    """
    params = {**DEFAULT_PARAMS, **(params or {})}
    bundle = QuantileBundle(feature_mode=feature_mode,
                            features=feature_columns(feature_mode))

    for horizon in horizons:
        X, y, meta = build_training_frame(obs, labels, horizon=horizon,
                                          feature_mode=feature_mode)
        if "split" not in meta.columns:
            raise ValueError("labels must carry a 'split' column for time-based training")

        is_train = (meta["split"] == "train").to_numpy()
        is_val = (meta["split"] == "val").to_numpy()
        X_tr, y_tr = X[is_train], y[is_train]
        X_va, y_va = X[is_val], y[is_val]
        bundle.train_rows[horizon] = int(is_train.sum())

        for q in QUANTILES:
            booster = lgb.train(
                {**params, "alpha": q},
                lgb.Dataset(X_tr, label=y_tr),
                num_boost_round=NUM_BOOST_ROUND,
                valid_sets=[lgb.Dataset(X_va, label=y_va)],
                callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            bundle.boosters[(horizon, q)] = booster
            bundle.best_iterations[(horizon, q)] = booster.best_iteration
            if verbose:
                print(f"  {feature_mode:20s} h={horizon:>2}h q={q:.2f}  "
                      f"trees={booster.best_iteration:>4}  train_rows={len(X_tr)}")

    return bundle


def feature_importance(bundle: QuantileBundle, horizon: int, quantile: float = 0.50,
                       top: int = 20) -> pd.DataFrame:
    """Gain-based importance for one booster -- used to sanity-check the model."""
    booster = bundle.boosters[(horizon, quantile)]
    imp = pd.DataFrame({
        "feature": booster.feature_name(),
        "gain": booster.feature_importance("gain"),
    })
    imp["share"] = imp["gain"] / imp["gain"].sum()
    return imp.sort_values("gain", ascending=False).head(top).reset_index(drop=True)


def both_modes(obs: pd.DataFrame, labels: pd.DataFrame, **kwargs):
    """Train the FR-F02 and FR-F03 variants for the PRD 16.3 comparison."""
    return {
        MODE_TRANSACTION: train_bundle(obs, labels, feature_mode=MODE_TRANSACTION, **kwargs),
        MODE_CONTENT: train_bundle(obs, labels, feature_mode=MODE_CONTENT, **kwargs),
    }
