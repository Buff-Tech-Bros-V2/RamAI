"""Manual forecast testing tool for RamAI.

Allows quick manual testing of trained forecast bundles with custom SKUs,
cutoffs, or comparison between calm and viral surge periods.

Examples:
    # Run interactive comparison between a calm and surge period
    python scripts/test_manual_forecast.py --compare-demo

    # Test a specific SKU at a specific timestamp
    python scripts/test_manual_forecast.py --sku SKU-001 --cutoff "2026-09-14 12:00"

    # Compare transaction vs content models for a specific SKU and cutoff
    python scripts/test_manual_forecast.py --sku SKU-002 --cutoff "2026-09-10 15:00" --compare-modes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.forecasting.evaluation import baseline_hourly_series
from apps.forecasting.features import (
    HORIZONS,
    MODE_CONTENT,
    MODE_TRANSACTION,
    latest_feature_row,
    load_observations,
)
from apps.forecasting.quantile_model import QuantileBundle


def run_single_forecast(
    bundle: QuantileBundle,
    obs: pd.DataFrame,
    sku_id: str,
    cutoff: pd.Timestamp,
    base_series: pd.DataFrame,
) -> pd.DataFrame:
    row = latest_feature_row(
        obs, sku_id=sku_id, cutoff=cutoff, feature_mode=bundle.feature_mode
    )

    base_match = base_series[
        (base_series["sku_id"] == sku_id) & (base_series["timestamp"] <= cutoff)
    ]
    base_val = (
        base_match["baseline_hourly"].iloc[-1] if not base_match.empty else float("nan")
    )

    records = []
    for h in HORIZONS:
        preds = bundle.predict(row, horizon=h)
        persist = (
            float(bundle.predict_persistence(row, horizon=h).iloc[0])
            if bundle.has_persistence(h)
            else float("nan")
        )
        p10 = float(preds["p10"].iloc[0])
        p50 = float(preds["p50"].iloc[0])
        p90 = float(preds["p90"].iloc[0])
        base_h = base_val * h if pd.notna(base_val) else float("nan")
        ratio_to_base = p50 / base_h if pd.notna(base_h) and base_h > 0 else float("nan")

        records.append({
            "horizon": f"{h}h",
            "P10": round(p10, 1),
            "P50": round(p50, 1),
            "P90": round(p90, 1),
            "persistence": f"{persist:.1%}" if pd.notna(persist) else "N/A",
            "baseline_exp": round(base_h, 1) if pd.notna(base_h) else "N/A",
            "ratio_vs_base": f"{ratio_to_base:.2f}x" if pd.notna(ratio_to_base) else "N/A",
        })
    return pd.DataFrame(records)


def print_forecast_table(df: pd.DataFrame, title: str) -> None:
    print("\n" + "-" * 75)
    print(title)
    print("-" * 75)
    print(df.to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manually test RamAI forecast models.")
    parser.add_argument("--sku", default="SKU-001", help="SKU ID to forecast (e.g. SKU-001)")
    parser.add_argument("--cutoff", default=None, help="Forecast cutoff timestamp (e.g. '2026-09-14 12:00')")
    parser.add_argument("--data", default="data", help="Data directory")
    parser.add_argument("--artifacts", default="artifacts", help="Artifacts directory")
    parser.add_argument("--compare-demo", action="store_true", help="Compare a calm period vs a surge period")
    parser.add_argument("--compare-modes", action="store_true", help="Compare transaction-only vs content model")
    args = parser.parse_args()

    data_dir = Path(args.data)
    art_dir = Path(args.artifacts)

    print("Loading data and model bundles ...")
    obs = load_observations(data_dir / "hourly_observations.parquet")
    base_series = baseline_hourly_series(obs)

    bundle_content = QuantileBundle.load(art_dir / f"forecast_{MODE_CONTENT}")
    bundle_tx = (
        QuantileBundle.load(art_dir / f"forecast_{MODE_TRANSACTION}")
        if args.compare_modes
        else None
    )

    tz = obs["timestamp"].dt.tz

    if args.compare_demo:
        # Pre-configured demo timestamps
        calm_ts = pd.to_datetime("2026-08-01 12:00:00").tz_localize(tz)
        surge_ts = pd.to_datetime("2026-09-14 12:00:00").tz_localize(tz)

        print("\n" + "=" * 75)
        print("RamAI MANUAL TEST: CALM vs SURGE DEMO COMPARISON")
        print("=" * 75)

        df_calm = run_single_forecast(bundle_content, obs, args.sku, calm_ts, base_series)
        print_forecast_table(
            df_calm, f"SCENARIO 1: NORMAL CALM PERIOD ({args.sku} at {calm_ts})"
        )

        df_surge = run_single_forecast(bundle_content, obs, args.sku, surge_ts, base_series)
        print_forecast_table(
            df_surge, f"SCENARIO 2: ACTIVE VIRAL SURGE PERIOD ({args.sku} at {surge_ts})"
        )
        print("\n" + "=" * 75)
        return

    # Specific cutoff mode
    if args.cutoff:
        cutoff = pd.to_datetime(args.cutoff)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize(tz)
    else:
        cutoff = obs["timestamp"].max()

    print("\n" + "=" * 75)
    print(f"RamAI MANUAL FORECAST: {args.sku} at {cutoff}")
    print("=" * 75)

    df_content = run_single_forecast(bundle_content, obs, args.sku, cutoff, base_series)
    print_forecast_table(df_content, f"Content-Enriched Model ({MODE_CONTENT})")

    if bundle_tx:
        df_tx = run_single_forecast(bundle_tx, obs, args.sku, cutoff, base_series)
        print_forecast_table(df_tx, f"Transaction-Only Model ({MODE_TRANSACTION})")


if __name__ == "__main__":
    main()
