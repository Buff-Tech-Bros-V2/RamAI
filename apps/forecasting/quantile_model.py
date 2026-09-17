"""LightGBM quantile forecast models (PRD FR-F02/F03/F04, 10.3).

One booster per (horizon, quantile), pooled across SKUs -- SKU identity is a
feature, not a reason to fit a separate small model. Two variants share this
code path and differ only in ``feature_mode``:

    transaction          FR-F02, transaction signals only
    transaction_content  FR-F03, plus the affiliate/content block

Target is cumulative fulfillment demand over the horizon, trained only on
label windows that were not stockout-censored (see features.build_training_frame).

Two things about *how* that target is fit matter more than the hyperparameters:

**Ratio target.** Boosters are fit on ``demand / (trailing_level * horizon)``
rather than on the raw count, and the prediction is multiplied back. A tree
cannot extrapolate past the leaf averages it saw in training, so an absolute
target makes it systematically under-call surges -- it has no way to say "3x
normal" for a SKU whose normal level it has never seen that high. On the ratio
scale that is one split. This removed most of the under-forecast bias
(+13% -> +4% on the training split) and improved WAPE at every horizon.

**Conformal intervals.** Raw quantile boosters undercovered (74% inside a
nominal 80% band). ``bundle.conformal`` holds one width correction per horizon,
fitted on the validation split only, that restores nominal coverage.

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
from .evaluation import baseline_hourly_series
from .scenarios import residual_blocks

MODEL_NAME = "lightgbm_quantile"
MODEL_VERSION = "forecast-v2"
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

# Denominator for the ratio target: a blend of the recent and the settled level,
# so a SKU mid-surge is not normalised by its own spike. Chosen on validation --
# the blend tied or beat rollmean_24 and rollmean_72 alone at every horizon.
SCALE_FEATURES = ("fulfillment_demand_rollmean_24", "fulfillment_demand_rollmean_72")
SCALE_FLOOR = 0.05          # units/hour; keeps a dead SKU from dividing by zero
TARGET_COVERAGE = 0.80      # nominal p10..p90 band

PERSISTENCE_PARAMS = {
    "objective": "binary",
    "metric": "auc",
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_data_in_leaf": 40,
    "feature_fraction": 0.85,
    "bagging_fraction": 0.85,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "num_threads": 0,
    "seed": 42,
}
PERSISTENCE_THRESHOLD = 1.2


def target_scale(X: pd.DataFrame, horizon: int) -> np.ndarray:
    """Units the ratio target is expressed in: expected demand at the current level."""
    level = np.zeros(len(X), dtype=float)
    for col in SCALE_FEATURES:
        level += X[col].to_numpy(dtype=float) / len(SCALE_FEATURES)
    return np.maximum(np.nan_to_num(level, nan=SCALE_FLOOR), SCALE_FLOOR) * horizon


@dataclass
class QuantileBundle:
    """All boosters for one feature mode: {(horizon, quantile): Booster}."""

    feature_mode: str
    boosters: dict = field(default_factory=dict)
    persistence_boosters: dict = field(default_factory=dict)
    residual_blocks: dict = field(default_factory=dict)
    features: list[str] = field(default_factory=list)
    train_rows: dict = field(default_factory=dict)
    best_iterations: dict = field(default_factory=dict)
    conformal: dict = field(default_factory=dict)   # horizon -> ratio-space width

    model_name: str = MODEL_NAME
    model_version: str = MODEL_VERSION
    feature_version: str = FEATURE_VERSION

    # ------------------------------------------------------------ predict --
    def predict(self, X: pd.DataFrame, horizon: int) -> pd.DataFrame:
        """Return a p10/p50/p90 frame, monotonically sorted across quantiles."""
        if horizon not in HORIZONS:
            raise ValueError(f"unknown horizon {horizon}; expected one of {HORIZONS}")

        X = X[self.features]
        scale = target_scale(X, horizon)
        widen = float(self.conformal.get(horizon, self.conformal.get(str(horizon), 0.0)))

        out = {}
        for q in QUANTILES:
            booster = self.boosters[(horizon, q)]
            ratio = np.asarray(
                booster.predict(X, num_iteration=booster.best_iteration or None), dtype=float
            )
            # Widen only the outer quantiles: the conformal correction is about
            # interval coverage, and shifting p50 would re-introduce bias.
            if q == min(QUANTILES):
                ratio = ratio - widen
            elif q == max(QUANTILES):
                ratio = ratio + widen
            out[f"p{int(q * 100)}"] = np.maximum(ratio * scale, 0.0)

        frame = pd.DataFrame(out, index=X.index)
        # quantile crossing is possible when boosters are fit independently;
        # sorting is the standard cheap fix and keeps p10 <= p50 <= p90.
        sorted_vals = np.sort(frame.to_numpy(), axis=1)
        return pd.DataFrame(sorted_vals, columns=["p10", "p50", "p90"], index=X.index)

    def has_persistence(self, horizon: int) -> bool:
        return horizon in self.persistence_boosters

    def predict_persistence(self, X: pd.DataFrame, horizon: int) -> pd.Series:
        """Return probability of surge persistence over the horizon (FR-F05)."""
        if horizon not in self.persistence_boosters:
            raise KeyError(f"no persistence booster for horizon {horizon}")
        booster = self.persistence_boosters[horizon]
        X_feat = X[self.features]
        prob = booster.predict(X_feat, num_iteration=booster.best_iteration or None)
        return pd.Series(np.clip(prob, 0.0, 1.0), index=X.index)

    # --------------------------------------------------------------- io ----
    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for (horizon, q), booster in self.boosters.items():
            booster.save_model(str(directory / f"h{horizon}_q{int(q * 100)}.txt"))
        for horizon, booster in self.persistence_boosters.items():
            booster.save_model(str(directory / f"h{horizon}_persistence.txt"))
        for horizon, blocks in self.residual_blocks.items():
            np.save(str(directory / f"h{horizon}_residuals.npy"), blocks)
        meta = {
            "feature_mode": self.feature_mode,
            "features": self.features,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "horizons": list(HORIZONS),
            "quantiles": list(QUANTILES),
            "has_persistence": {str(h): (h in self.persistence_boosters) for h in HORIZONS},
            "train_rows": {str(k): v for k, v in self.train_rows.items()},
            "best_iterations": {f"{h}_{int(q * 100)}": v
                                for (h, q), v in self.best_iterations.items()},
            "target": "ratio_to_trailing_level",
            "scale_features": list(SCALE_FEATURES),
            "conformal": {str(h): v for h, v in self.conformal.items()},
        }
        (directory / "bundle.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return directory

    @classmethod
    def load(cls, directory: str | Path) -> "QuantileBundle":
        directory = Path(directory)
        meta = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
        boosters = {}
        persistence_boosters = {}
        residual_blocks_map = {}
        for horizon in meta["horizons"]:
            for q in meta["quantiles"]:
                path = directory / f"h{horizon}_q{int(q * 100)}.txt"
                boosters[(horizon, q)] = lgb.Booster(model_file=str(path))
            p_path = directory / f"h{horizon}_persistence.txt"
            if p_path.exists():
                persistence_boosters[horizon] = lgb.Booster(model_file=str(p_path))
            r_path = directory / f"h{horizon}_residuals.npy"
            if r_path.exists():
                residual_blocks_map[horizon] = np.load(str(r_path))
        return cls(
            feature_mode=meta["feature_mode"],
            boosters=boosters,
            persistence_boosters=persistence_boosters,
            residual_blocks=residual_blocks_map,
            features=meta["features"],
            train_rows={k: v for k, v in meta.get("train_rows", {}).items()},
            conformal={int(h): v for h, v in meta.get("conformal", {}).items()},
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

    Boosters learn the ratio target (see module docstring) and the val split
    drives both early stopping and the conformal interval width. The test split
    is never touched here.
    """
    params = {**DEFAULT_PARAMS, **(params or {})}
    bundle = QuantileBundle(feature_mode=feature_mode,
                            features=feature_columns(feature_mode))
    base_df = baseline_hourly_series(obs)

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

        # Fit on the ratio scale; predictions are multiplied back in predict().
        scale_tr = target_scale(X_tr, horizon)
        scale_va = target_scale(X_va, horizon)
        r_tr = y_tr.to_numpy(dtype=float) / scale_tr
        r_va = y_va.to_numpy(dtype=float) / scale_va

        for q in QUANTILES:
            booster = lgb.train(
                {**params, "alpha": q},
                lgb.Dataset(X_tr, label=r_tr),
                num_boost_round=NUM_BOOST_ROUND,
                valid_sets=[lgb.Dataset(X_va, label=r_va)],
                callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            bundle.boosters[(horizon, q)] = booster
            bundle.best_iterations[(horizon, q)] = booster.best_iteration
            if verbose:
                print(f"  {feature_mode:20s} h={horizon:>2}h q={q:.2f}  "
                      f"trees={booster.best_iteration:>4}  train_rows={len(X_tr)}")

        bundle.conformal[horizon] = _conformal_width(bundle, X_va, r_va, horizon)
        if verbose:
            print(f"  {feature_mode:20s} h={horizon:>2}h conformal width="
                  f"{bundle.conformal[horizon]:.3f} (ratio units)")

        # Fit persistence classifier (FR-F05)
        meta_base = meta.merge(base_df, on=["timestamp", "sku_id"], how="left")
        rate_tr = y_tr.to_numpy(dtype=float) / horizon
        base_tr = meta_base.loc[is_train, "baseline_hourly"].to_numpy(dtype=float)
        ok_tr = np.isfinite(base_tr) & (base_tr > 0)

        rate_va = y_va.to_numpy(dtype=float) / horizon
        base_va = meta_base.loc[is_val, "baseline_hourly"].to_numpy(dtype=float)
        ok_va = np.isfinite(base_va) & (base_va > 0)

        persist_tr = (rate_tr >= PERSISTENCE_THRESHOLD * base_tr).astype(int)
        persist_va = (rate_va >= PERSISTENCE_THRESHOLD * base_va).astype(int)

        if ok_tr.sum() > 0 and ok_va.sum() > 0:
            p_booster = lgb.train(
                PERSISTENCE_PARAMS,
                lgb.Dataset(X_tr[ok_tr], label=persist_tr[ok_tr]),
                num_boost_round=NUM_BOOST_ROUND,
                valid_sets=[lgb.Dataset(X_va[ok_va], label=persist_va[ok_va])],
                callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            bundle.persistence_boosters[horizon] = p_booster
            if verbose:
                print(f"  {feature_mode:20s} h={horizon:>2}h persistence  "
                      f"trees={p_booster.best_iteration:>4}")

        # Extract empirical residual blocks from validation predictions
        p50_booster = bundle.boosters[(horizon, 0.50)]
        pred_r_va = np.asarray(
            p50_booster.predict(X_va, num_iteration=p50_booster.best_iteration or None),
            dtype=float,
        )
        pred_p50_va = np.maximum(pred_r_va * scale_va, 0.0)
        bundle.residual_blocks[horizon] = residual_blocks(
            y_va.to_numpy(dtype=float), pred_p50_va, block_hours=6
        )

    return bundle


def _conformal_width(bundle: QuantileBundle, X_val: pd.DataFrame,
                     ratio_val: np.ndarray, horizon: int) -> float:
    """Interval widening that gives nominal coverage on the validation split.

    Split-conformal (CQR): the conformity score is how far outside the raw band
    each validation point fell; its (1-alpha) empirical quantile is the width
    that would have covered that share of them. Fitted on val, never on test.
    """
    lo_booster = bundle.boosters[(horizon, min(QUANTILES))]
    hi_booster = bundle.boosters[(horizon, max(QUANTILES))]
    lo = np.asarray(lo_booster.predict(
        X_val, num_iteration=lo_booster.best_iteration or None), dtype=float)
    hi = np.asarray(hi_booster.predict(
        X_val, num_iteration=hi_booster.best_iteration or None), dtype=float)

    scores = np.maximum(lo - ratio_val, ratio_val - hi)
    n = len(scores)
    if n == 0:
        return 0.0
    rank = min(int(np.ceil((n + 1) * TARGET_COVERAGE)) - 1, n - 1)
    return float(max(np.sort(scores)[rank], 0.0))


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
