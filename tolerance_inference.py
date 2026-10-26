# tolerance_inference.py

import os
import logging
import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def build_block_bin_map(bins_df):

    rows = []

    for bin_id, row in bins_df.iterrows():

        for block in range(int(row["start_block"]), int(row["end_block"]) + 1):
            rows.append({
                "block": block,
                "bin_id": bin_id
            })

    return pd.DataFrame(rows)


def infer_tolerance(swaps_df, failed_df, bins_df):

    # Build mapping
    block_bin_df = build_block_bin_map(bins_df)

    # Merge bin_id
    swaps_df = swaps_df.merge(block_bin_df, on="block", how="left")
    failed_df = failed_df.merge(block_bin_df, on="block", how="left")

    results = []

    grouped_swaps = swaps_df.groupby("bin_id")
    grouped_failed = failed_df.groupby("bin_id")

    valid_bins = sorted(
        set(swaps_df["bin_id"].dropna()).intersection(
            set(failed_df["bin_id"].dropna())
        )
    )

    for bin_id in valid_bins:

        swaps_bin = grouped_swaps.get_group(bin_id)
        failed_bin = grouped_failed.get_group(bin_id)

        total_router = failed_bin["router_txs"].sum()
        total_failed = failed_bin["failed_txs"].sum()

        if total_router == 0:
            continue

        rejection_rate = total_failed / total_router

        slippages = swaps_bin["local_slippage"].dropna().values

        if len(slippages) < 5:
            continue

        percentile = 1 - rejection_rate
        percentile = max(0.0, min(1.0, percentile))

        tolerance = np.quantile(slippages, percentile)

        results.append({
            "bin_id": bin_id,
            "start_block": bins_df.loc[bin_id, "start_block"],
            "end_block": bins_df.loc[bin_id, "end_block"],
            "rejection_rate": rejection_rate,
            "tolerance": tolerance,
            "swap_count": len(slippages),
            "router_txs": total_router,
            "failed_txs": total_failed
        })

    return pd.DataFrame(results)


if __name__ == "__main__":

    from config import OUTPUT_DIR  # PATH-1: same folder the pipeline uses
    logger.info("Loading swaps, failed tx and bins")

    swaps_df = pd.read_parquet(f"{OUTPUT_DIR}/swaps.parquet")
    failed_df = pd.read_parquet(f"{OUTPUT_DIR}/failed_transactions.parquet")
    bins_df = pd.read_parquet(f"{OUTPUT_DIR}/bins.parquet")

    logger.info("Inferring tolerance per bin")

    tolerance_df = infer_tolerance(swaps_df, failed_df, bins_df)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    tolerance_df.to_parquet(f"{OUTPUT_DIR}/tolerance_bins.parquet", index=False)
    tolerance_df.to_csv(f"{OUTPUT_DIR}/tolerance_bins.csv", index=False)

    logger.info("Tolerance inference completed")
    logger.info(f"Bins inferred: {len(tolerance_df)}")
