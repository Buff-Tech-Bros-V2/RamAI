"""Tests for the forecasting feature pipeline (PRD 10.3).

These run on plain DataFrames and mock objects -- no database, no Django
settings -- so they stay fast and deterministic.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from apps.forecasting import features as F

TZ = timezone(timedelta(hours=7))


def make_obs(n_hours=400, skus=("SKU-001", "SKU-002"), content=True, seed=0):
    """Minimal but schema-complete hourly observation frame."""
    rng = np.random.default_rng(seed)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)
    rows = []
    for si, sku in enumerate(skus):
        for t in range(n_hours):
            ts = start + timedelta(hours=t)
            orders = int(5 + si * 2 + (t % 24) // 6 + rng.integers(0, 3))
            rows.append({
                "timestamp": ts,
                "shop_id": "SHOP-DEMO-01",
                "sku_id": sku,
                "product_category": "packaged_food",
                "orders_created": orders,
                "orders_cancelled_pre_ship": int(rng.integers(0, 2)),
                "orders_shipped": orders,
                "orders_delivered": orders,
                "orders_returned": 0,
                "stock_on_hand": 500 - t % 100,
                "incoming_stock": 0,
                "stockout_flag": 1 if t % 97 == 0 else 0,
                "price": 38000.0,
                "promotion_flag": 1 if 100 <= t < 130 else 0,
                "product_views": float(orders * 40) if content else np.nan,
                "affiliate_orders": float(orders // 3) if content else np.nan,
                "active_affiliates": 4.0 if content else np.nan,
                "content_view_velocity": float(300 + orders * 10) if content else np.nan,
            })
    return pd.DataFrame(rows)


def make_labels(obs, horizons=(24, 48, 72)):
    out = obs[["timestamp", "sku_id"]].copy()
    rng = np.random.default_rng(1)
    for h in horizons:
        out[f"target_demand_{h}h"] = rng.uniform(50, 200, len(out))
        out[f"target_censored_{h}h"] = 0
    # a few censored and a few unlabelled rows
    out.loc[out.index[:10], "target_censored_24h"] = 1
    out.loc[out.index[-5:], "target_demand_24h"] = np.nan
    return out


# ---------------------------------------------------------------- leakage --

def test_features_never_use_future_rows():
    """Perturbing a future observation must not change earlier feature rows."""
    obs = make_obs()
    base = F.build_features(obs)

    tampered = obs.copy()
    last_idx = tampered.index[tampered["sku_id"] == "SKU-001"][-1]
    tampered.loc[last_idx, "orders_created"] = 9999

    after = F.build_features(tampered)
    cols = F.feature_columns(F.MODE_CONTENT)

    a = base[base["sku_id"] == "SKU-001"].iloc[:-1][cols].reset_index(drop=True)
    b = after[after["sku_id"] == "SKU-001"].iloc[:-1][cols].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_lag_features_do_not_bleed_across_skus():
    obs = make_obs(n_hours=50)
    feats = F.build_features(obs)
    for sku in obs["sku_id"].unique():
        g = feats[feats["sku_id"] == sku].sort_values("timestamp").reset_index(drop=True)
        src = obs[obs["sku_id"] == sku].sort_values("timestamp").reset_index(drop=True)
        assert pd.isna(g.loc[0, "orders_created_lag_1"])
        assert g.loc[1, "orders_created_lag_1"] == src.loc[0, "orders_created"]


def test_rolling_mean_includes_current_hour_only_backwards():
    obs = make_obs(n_hours=30, skus=("SKU-001",))
    feats = F.build_features(obs).sort_values("timestamp").reset_index(drop=True)
    src = obs.sort_values("timestamp").reset_index(drop=True)
    expected = src.loc[0:5, "orders_created"].mean()
    assert feats.loc[5, "orders_created_rollmean_6"] == pytest.approx(expected)


# --------------------------------------------------------------- content ---

def test_transaction_mode_excludes_content_columns():
    cols = F.feature_columns(F.MODE_TRANSACTION)
    assert not any("content" in c or "affiliate" in c or "views" in c for c in cols)
    assert "orders_created_lag_1" in cols


def test_content_mode_includes_content_columns():
    cols = F.feature_columns(F.MODE_CONTENT)
    assert "content_view_velocity" in cols
    assert "affiliate_orders" in cols


def test_runs_with_all_content_null_and_flags_it():
    """FR-D06: the pipeline must work when every content feature is null."""
    obs = make_obs(content=False)
    feats = F.build_features(obs, feature_mode=F.MODE_CONTENT)
    assert len(feats) == len(obs)
    assert (feats["content_features_used"] == 0).all()
    assert feats["content_view_velocity"].isna().all()


def test_content_columns_absent_entirely_from_source_frame():
    """A transaction-only feed has no content columns at all, not even null ones."""
    obs = make_obs(n_hours=60, skus=("SKU-001",)).drop(columns=F.CONTENT_COLUMNS)
    feats = F.build_features(obs, feature_mode=F.MODE_CONTENT)
    assert len(feats) == len(obs)
    assert (feats["content_features_used"] == 0).all()
    assert feats["content_view_velocity"].isna().all()
    assert list(feats.columns)[2:] == F.feature_columns(F.MODE_CONTENT)


def test_transaction_mode_works_when_content_columns_absent():
    obs = make_obs(n_hours=60, skus=("SKU-001",)).drop(columns=F.CONTENT_COLUMNS)
    feats = F.build_features(obs, feature_mode=F.MODE_TRANSACTION)
    assert (feats["content_features_used"] == 0).all()


def test_content_available_rows_are_flagged_used():
    obs = make_obs(content=True)
    feats = F.build_features(obs, feature_mode=F.MODE_CONTENT)
    assert (feats["content_features_used"] == 1).all()


def test_partial_content_blackout_flags_only_affected_rows():
    obs = make_obs(n_hours=60, skus=("SKU-001",))
    blackout = obs.index[10:20]
    for col in F.CONTENT_COLUMNS:
        obs.loc[blackout, col] = np.nan
    feats = F.build_features(obs, feature_mode=F.MODE_CONTENT)
    flags = feats.sort_values("timestamp")["content_features_used"].to_numpy()
    assert flags[10:20].sum() == 0
    assert flags[:10].all() and flags[20:].all()


def test_transaction_mode_ignores_content_even_when_present():
    obs = make_obs(content=True)
    feats = F.build_features(obs, feature_mode=F.MODE_TRANSACTION)
    assert (feats["content_features_used"] == 0).all()


# --------------------------------------------------------- calendar/price --

def test_calendar_and_price_features_present():
    obs = make_obs(n_hours=200, skus=("SKU-001",))
    feats = F.build_features(obs).sort_values("timestamp").reset_index(drop=True)
    assert feats.loc[0, "hour"] == 0
    assert set(["hour_sin", "hour_cos", "dow_sin", "dow_cos"]).issubset(feats.columns)
    assert feats["promotion_flag"].max() == 1
    # price relative to its own trailing mean is ~1 when price is flat
    assert feats["price_vs_trailing_mean"].dropna().between(0.99, 1.01).all()


def test_sku_identity_is_a_categorical_feature():
    obs = make_obs()
    feats = F.build_features(obs)
    assert "sku_id_code" in F.feature_columns(F.MODE_CONTENT)
    assert feats["sku_id_code"].nunique() == 2


# --------------------------------------------------------------- training --

def test_build_training_frame_drops_censored_and_unlabelled_rows():
    obs = make_obs()
    labels = make_labels(obs)
    X, y, meta = F.build_training_frame(obs, labels, horizon=24)
    assert len(X) == len(y) == len(meta)
    assert y.notna().all()
    assert (meta["target_censored_24h"] == 0).all()
    assert len(X) < len(obs)


def test_build_training_frame_can_keep_censored_rows():
    obs = make_obs()
    labels = make_labels(obs)
    X_drop, _, _ = F.build_training_frame(obs, labels, horizon=24)
    X_keep, _, _ = F.build_training_frame(obs, labels, horizon=24, drop_censored=False)
    assert len(X_keep) > len(X_drop)


def test_build_training_frame_returns_only_feature_columns():
    obs = make_obs()
    labels = make_labels(obs)
    X, _, _ = F.build_training_frame(obs, labels, horizon=48, feature_mode=F.MODE_TRANSACTION)
    assert list(X.columns) == F.feature_columns(F.MODE_TRANSACTION)


def test_build_training_frame_rejects_unknown_horizon():
    obs = make_obs()
    labels = make_labels(obs)
    with pytest.raises(ValueError, match="horizon"):
        F.build_training_frame(obs, labels, horizon=99)


def test_build_features_rejects_unknown_mode():
    with pytest.raises(ValueError, match="feature_mode"):
        F.build_features(make_obs(n_hours=30), feature_mode="nonsense")


def test_build_features_requires_schema_columns():
    obs = make_obs(n_hours=30).drop(columns=["stockout_flag"])
    with pytest.raises(ValueError, match="stockout_flag"):
        F.build_features(obs)


# ---------------------------------------------------------------- cutoff ---

def test_latest_feature_row_returns_single_row_at_cutoff():
    obs = make_obs(n_hours=300, skus=("SKU-001",))
    cutoff = obs["timestamp"].iloc[200]
    row = F.latest_feature_row(obs, sku_id="SKU-001", cutoff=cutoff)
    assert len(row) == 1
    assert list(row.columns) == F.feature_columns(F.MODE_CONTENT)


def test_latest_feature_row_ignores_rows_after_cutoff():
    obs = make_obs(n_hours=300, skus=("SKU-001",))
    cutoff = obs["timestamp"].iloc[200]
    row = F.latest_feature_row(obs, sku_id="SKU-001", cutoff=cutoff)

    tampered = obs.copy()
    tampered.loc[tampered.index[250:], "orders_created"] = 9999
    row2 = F.latest_feature_row(tampered, sku_id="SKU-001", cutoff=cutoff)
    pd.testing.assert_frame_equal(row, row2)


def test_latest_feature_row_raises_when_no_history():
    obs = make_obs(n_hours=300, skus=("SKU-001",))
    early = obs["timestamp"].iloc[0] - timedelta(hours=5)
    with pytest.raises(ValueError, match="no observations"):
        F.latest_feature_row(obs, sku_id="SKU-001", cutoff=early)


# ---------------------------------------------------------- django adapter --

def test_observations_to_frame_maps_model_objects():
    """Mocked queryset rows -- no DB needed (prefer mocks over integration)."""
    ts = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)
    rows = [
        SimpleNamespace(
            sku_id="SKU-001", timestamp=ts + timedelta(hours=i),
            orders_created=5, orders_cancelled_pre_ship=0, orders_shipped=5,
            orders_delivered=5, orders_returned=0, stock_on_hand=100,
            incoming_stock=0, stockout_flag=False, price=38000, promotion_flag=False,
            product_views=200, affiliate_orders=1, active_affiliates=3,
            content_view_velocity=310.0,
        )
        for i in range(3)
    ]
    df = F.observations_to_frame(rows, product_category="packaged_food")
    assert len(df) == 3
    assert df["stockout_flag"].dtype.kind in "iu"
    assert df["price"].dtype.kind == "f"
    assert set(F.REQUIRED_COLUMNS).issubset(df.columns)


def test_observations_to_frame_handles_null_content():
    ts = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)
    rows = [
        SimpleNamespace(
            sku_id="SKU-001", timestamp=ts, orders_created=5,
            orders_cancelled_pre_ship=0, orders_shipped=5, orders_delivered=5,
            orders_returned=0, stock_on_hand=100, incoming_stock=0,
            stockout_flag=True, price=38000, promotion_flag=True,
            product_views=None, affiliate_orders=None, active_affiliates=None,
            content_view_velocity=None,
        )
    ]
    df = F.observations_to_frame(rows)
    assert df["content_view_velocity"].isna().all()
    assert df.loc[0, "stockout_flag"] == 1


def test_observations_to_frame_rejects_empty():
    with pytest.raises(ValueError, match="no observations"):
        F.observations_to_frame([])


# ------------------------------------------------------------------ misc ---

def test_feature_version_is_exposed():
    assert isinstance(F.FEATURE_VERSION, str) and F.FEATURE_VERSION


def test_load_observations_reads_generated_parquet(tmp_path):
    obs = make_obs(n_hours=30)
    p = tmp_path / "obs.parquet"
    obs.to_parquet(p, index=False)
    loaded = F.load_observations(p)
    assert len(loaded) == len(obs)
    assert set(F.REQUIRED_COLUMNS).issubset(loaded.columns)
