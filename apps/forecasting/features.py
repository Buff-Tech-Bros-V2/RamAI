"""Feature pipeline for the VIRALCAST quantile forecast (PRD 10.3).

Design rules that the tests pin down:

* **No leakage.** Every feature at cutoff ``t`` is built only from observations
  at or before ``t``. Labels live in a separate frame and always describe the
  window strictly after ``t``.
* **Two feature modes.** ``MODE_TRANSACTION`` uses transaction signals only;
  ``MODE_CONTENT`` adds the affiliate/content block. Training both from the
  same code path is what makes the PRD 16.3 comparison honest.
* **Content may be entirely absent** (FR-D06). Content columns stay as NaN --
  LightGBM handles missing natively -- and ``content_features_used`` records
  whether the row actually had a signal.
* **Pooled across SKUs.** ``sku_id``/``product_category`` enter as integer codes;
  we do not fit one small model per SKU.

The pipeline works on plain DataFrames so it can be driven either from the
generated parquet (offline training) or from a Django queryset (inference):

    df = load_observations("data/hourly_observations.parquet")
    X, y, meta = build_training_frame(df, labels, horizon=48)

    df = observations_to_frame(sku.observations.filter(timestamp__lte=cutoff))
    row = latest_feature_row(df, sku_id=sku.sku_id, cutoff=cutoff)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

FEATURE_VERSION = "features-v2"

MODE_TRANSACTION = "transaction"
MODE_CONTENT = "transaction_content"
FEATURE_MODES = (MODE_TRANSACTION, MODE_CONTENT)

HORIZONS = (24, 48, 72)

LAGS = (1, 2, 3, 6, 12, 24, 48, 72, 168)
ROLL_WINDOWS = (6, 24, 72)
CONTENT_LAGS = (1, 3, 6, 24, 48)

CONTENT_COLUMNS = [
    "product_views",
    "affiliate_orders",
    "active_affiliates",
    "content_view_velocity",
]

REQUIRED_COLUMNS = [
    "timestamp", "sku_id", "orders_created", "orders_cancelled_pre_ship",
    "stock_on_hand", "stockout_flag", "price", "promotion_flag",
]

# Series we build lag/rolling features on.
_BASE_SERIES = ("orders_created", "fulfillment_demand")


# --------------------------------------------------------------------------
# Loading / adapting
# --------------------------------------------------------------------------

def load_observations(path: str | Path) -> pd.DataFrame:
    """Read the generated hourly observations (parquet or csv)."""
    path = Path(path)
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(
        path, parse_dates=["timestamp"]
    )
    _require_columns(df)
    return df


def observations_to_frame(observations, product_category: str = "") -> pd.DataFrame:
    """Convert Django ``HourlyObservation`` rows into the pipeline's frame.

    Accepts any iterable of objects with the PRD 9.1 attributes, so it can be
    exercised with mocks and never needs a database.
    """
    rows = list(observations)
    if not rows:
        raise ValueError("no observations supplied")

    def _f(value):
        return np.nan if value is None else float(value)

    df = pd.DataFrame([{
        "timestamp": o.timestamp,
        "sku_id": o.sku_id,
        "product_category": getattr(o, "product_category", product_category),
        "orders_created": int(o.orders_created),
        "orders_cancelled_pre_ship": int(o.orders_cancelled_pre_ship),
        "orders_shipped": int(o.orders_shipped),
        "orders_delivered": int(o.orders_delivered),
        "orders_returned": int(o.orders_returned),
        "stock_on_hand": int(o.stock_on_hand),
        "incoming_stock": int(o.incoming_stock),
        "stockout_flag": int(bool(o.stockout_flag)),
        "price": float(o.price),
        "promotion_flag": int(bool(o.promotion_flag)),
        "product_views": _f(o.product_views),
        "affiliate_orders": _f(o.affiliate_orders),
        "active_affiliates": _f(o.active_affiliates),
        "content_view_velocity": _f(o.content_view_velocity),
    } for o in rows])
    return df


# --------------------------------------------------------------------------
# Feature construction
# --------------------------------------------------------------------------

def feature_columns(feature_mode: str = MODE_CONTENT) -> list[str]:
    """The exact, ordered column list a model of this mode is trained on."""
    _check_mode(feature_mode)

    cols: list[str] = []
    for series in _BASE_SERIES:
        cols += [f"{series}_lag_{lag}" for lag in LAGS]
        for w in ROLL_WINDOWS:
            cols += [f"{series}_rollmean_{w}", f"{series}_rollstd_{w}"]
        cols += [f"{series}_growth_24", f"{series}_growth_72"]

    cols += [
        "hour", "dow", "hour_sin", "hour_cos", "dow_sin", "dow_cos", "is_weekend",
        "price", "price_vs_trailing_mean", "promotion_flag",
        # stock_on_hand / stock_cover_hours are deliberately NOT features: they
        # proxy the replenishment rule rather than demand, dominated gain at 30%,
        # and removing them improved WAPE for both modes (0.253 -> 0.232 at 48h).
        # stockout_flag stays -- it tells the model the history was truncated.
        "stockout_flag", "stockout_share_24",
        "sku_id_code", "product_category_code",
    ]

    if feature_mode == MODE_CONTENT:
        cols += list(CONTENT_COLUMNS)
        cols += [f"{c}_lag_{lag}" for c in CONTENT_COLUMNS for lag in CONTENT_LAGS]
        cols += [
            "content_view_velocity_rollmean_6",
            "content_view_velocity_growth_6",
            "affiliate_order_share",
            "views_per_order",
            "content_features_used",
        ]
    return cols


def build_features(obs: pd.DataFrame, feature_mode: str = MODE_CONTENT) -> pd.DataFrame:
    """Return one feature row per input observation row.

    The output keeps ``timestamp``/``sku_id`` alongside the feature columns so
    callers can join labels or slice by cutoff.
    """
    _check_mode(feature_mode)
    _require_columns(obs)

    df = obs.sort_values(["sku_id", "timestamp"]).reset_index(drop=True).copy()
    df["fulfillment_demand"] = (
        df["orders_created"] - df["orders_cancelled_pre_ship"]
    ).astype(float)
    df["stockout_flag"] = df["stockout_flag"].astype(int)
    df["promotion_flag"] = df["promotion_flag"].astype(int)

    g = df.groupby("sku_id", sort=False)

    # --- lag / rolling on the demand series ------------------------------
    for series in _BASE_SERIES:
        s = g[series]
        for lag in LAGS:
            df[f"{series}_lag_{lag}"] = s.shift(lag)
        for w in ROLL_WINDOWS:
            df[f"{series}_rollmean_{w}"] = s.transform(
                lambda x, w=w: x.rolling(w, min_periods=1).mean()
            )
            df[f"{series}_rollstd_{w}"] = s.transform(
                lambda x, w=w: x.rolling(w, min_periods=2).std()
            )
        # growth = recent level vs. the level one/three days back
        df[f"{series}_growth_24"] = _ratio(
            df[f"{series}_rollmean_6"], df[f"{series}_rollmean_24"]
        )
        df[f"{series}_growth_72"] = _ratio(
            df[f"{series}_rollmean_24"], df[f"{series}_rollmean_72"]
        )

    # --- calendar --------------------------------------------------------
    ts = pd.DatetimeIndex(df["timestamp"])
    df["hour"] = ts.hour
    df["dow"] = ts.dayofweek
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    df["dow_sin"] = np.sin(2 * np.pi * df["dow"] / 7)
    df["dow_cos"] = np.cos(2 * np.pi * df["dow"] / 7)
    df["is_weekend"] = (df["dow"] >= 5).astype(int)

    # --- price / inventory ----------------------------------------------
    df["price"] = df["price"].astype(float)
    price_trailing = g["price"].transform(
        lambda x: x.rolling(24 * 7, min_periods=1).mean()
    )
    df["price_vs_trailing_mean"] = _ratio(df["price"], price_trailing)
    df["stock_cover_hours"] = _ratio(
        df["stock_on_hand"], df["orders_created_rollmean_24"]
    )
    df["stockout_share_24"] = g["stockout_flag"].transform(
        lambda x: x.rolling(24, min_periods=1).mean()
    )

    # --- identity --------------------------------------------------------
    df["sku_id_code"] = df["sku_id"].astype("category").cat.codes
    category = df["product_category"] if "product_category" in df else pd.Series("", index=df.index)
    df["product_category_code"] = category.astype("category").cat.codes

    # --- content block ---------------------------------------------------
    has_content = _content_present(df)
    if feature_mode == MODE_CONTENT:
        for col in CONTENT_COLUMNS:
            if col not in df:
                df[col] = np.nan
            df[col] = df[col].astype(float)
        cg = df.groupby("sku_id", sort=False)
        for col in CONTENT_COLUMNS:
            for lag in CONTENT_LAGS:
                df[f"{col}_lag_{lag}"] = cg[col].shift(lag)
        df["content_view_velocity_rollmean_6"] = cg["content_view_velocity"].transform(
            lambda x: x.rolling(6, min_periods=1).mean()
        )
        df["content_view_velocity_growth_6"] = _ratio(
            df["content_view_velocity"], df["content_view_velocity_rollmean_6"]
        )
        df["affiliate_order_share"] = _ratio(df["affiliate_orders"], df["orders_created"])
        df["views_per_order"] = _ratio(df["product_views"], df["orders_created"])
        df["content_features_used"] = has_content.astype(int)
    else:
        # transaction-only models must not see the content block at all
        df = df.drop(columns=[c for c in CONTENT_COLUMNS if c in df])
        df["content_features_used"] = 0

    keep = ["timestamp", "sku_id"] + feature_columns(feature_mode)
    if feature_mode == MODE_TRANSACTION:
        keep.append("content_features_used")
    return df[keep]


def build_training_frame(
    obs: pd.DataFrame,
    labels: pd.DataFrame,
    horizon: int,
    feature_mode: str = MODE_CONTENT,
    drop_censored: bool = True,
):
    """Join features to one horizon's label and return ``(X, y, meta)``.

    Rows without a label, and (by default) rows whose label window overlaps a
    stockout, are dropped -- otherwise the model learns that an empty shelf
    means zero demand (PRD 10.2, acceptance criteria).
    """
    if horizon not in HORIZONS:
        raise ValueError(f"unknown horizon {horizon}; expected one of {HORIZONS}")
    _check_mode(feature_mode)

    target_col = f"target_demand_{horizon}h"
    censored_col = f"target_censored_{horizon}h"

    feats = build_features(obs, feature_mode=feature_mode)
    label_cols = ["timestamp", "sku_id", target_col, censored_col]
    extra = [c for c in ("split",) if c in labels.columns]
    merged = feats.merge(labels[label_cols + extra], on=["timestamp", "sku_id"], how="inner")

    keep = merged[target_col].notna()
    if drop_censored:
        keep &= merged[censored_col].fillna(1).astype(int) == 0
    merged = merged[keep].reset_index(drop=True)

    X = merged[feature_columns(feature_mode)]
    y = merged[target_col]
    meta = merged[["timestamp", "sku_id", censored_col] + extra]
    return X, y, meta


def latest_feature_row(
    obs: pd.DataFrame,
    sku_id: str,
    cutoff,
    feature_mode: str = MODE_CONTENT,
) -> pd.DataFrame:
    """One feature row for ``sku_id`` at ``cutoff``, built only from history.

    Rows after the cutoff are dropped *before* any feature is computed, so an
    inference call can never see the future even if the caller passes a full
    history frame.
    """
    _check_mode(feature_mode)
    history = obs[(obs["sku_id"] == sku_id) & (obs["timestamp"] <= cutoff)]
    if history.empty:
        raise ValueError(f"no observations for {sku_id} at or before {cutoff}")

    feats = build_features(history, feature_mode=feature_mode)
    last = feats.sort_values("timestamp").iloc[[-1]]
    return last[feature_columns(feature_mode)].reset_index(drop=True)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

def _check_mode(feature_mode: str) -> None:
    if feature_mode not in FEATURE_MODES:
        raise ValueError(
            f"unknown feature_mode {feature_mode!r}; expected one of {FEATURE_MODES}"
        )


def _require_columns(df: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"observations missing required columns: {', '.join(missing)}")


def _ratio(numerator, denominator):
    """Safe elementwise ratio; undefined where the denominator is ~0."""
    den = pd.Series(denominator).astype(float).to_numpy()
    num = pd.Series(numerator).astype(float).to_numpy()
    out = np.divide(num, den, out=np.full_like(num, np.nan, dtype=float),
                    where=np.abs(den) > 1e-9)
    return pd.Series(out, index=pd.Series(numerator).index)


def _content_present(df: pd.DataFrame) -> pd.Series:
    cols = [c for c in CONTENT_COLUMNS if c in df.columns]
    if not cols:
        return pd.Series(False, index=df.index)
    return df[cols].notna().any(axis=1)
