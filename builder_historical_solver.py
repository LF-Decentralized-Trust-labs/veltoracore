#!/usr/bin/env python3

import pandas as pd
import numpy as np
import logging
from config import *
from instances import generate_lps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --------------------------------------------------
# Build users from historical swaps
# --------------------------------------------------

def build_users_historical(swaps_bin, tolerance_value):
    """
    Convert historical swaps into solver user intents.

    tolerance_value: inferred slippage tolerance per bin
    """

    tolerance_value = max(tolerance_value, 0.001)

    users = []

    for _, row in swaps_bin.iterrows():

        q = row["eth_amount"]          # always >= 0 (extractor stores abs value)
        P_exec = row["price"]

        # DIRECTION-1: the side comes from `direction`, written from the POOL's
        # point of view by swap_extractor ("buy" = the pool received ETH, i.e.
        # the USER SOLD ETH). Branching on q > 0 turned every swap into a buy.
        user_buys_eth = (row["direction"] == "sell")

        if user_buys_eth:
            # user buying ETH
            users.append({
                "q": abs(q),
                "p_min": None,
                "p_max": P_exec * (1 + tolerance_value)
            })
        else:
            # user selling ETH
            users.append({
                "q": -abs(q),
                "p_min": P_exec * (1 - tolerance_value),
                "p_max": None
            })

    return users

# --------------------------------------------------
# Compute total capital from liquidity snapshot
# --------------------------------------------------

def compute_total_capital(liq_row):
    """
    Reconstruct total USD capital from V2 + V3 pools.
    """

    # Use V2 price as reference price (they are very close)
    P = liq_row["price_v2"]

    capital = (
        liq_row["v2_eth"] * P + liq_row["v2_usdc"] +
        liq_row["v3_005_eth"] * P + liq_row["v3_005_usdc"] +
        liq_row["v3_03_eth"] * P + liq_row["v3_03_usdc"]
    )

    return capital


# --------------------------------------------------
# Main historical instance builder
# --------------------------------------------------

def build_historical_instances():

    swaps = pd.read_parquet("historical_data/swaps.parquet")
    bins = pd.read_parquet("historical_data/bins.parquet")
    liquidity = pd.read_parquet("historical_data/liquidity.parquet")
    tolerance = pd.read_parquet("historical_data/tolerance_bins.parquet")

    logger.info(f"Swaps rows: {len(swaps)}")
    logger.info(f"Bins: {len(bins)}")

    instances = []

    for _, bin_row in bins.iterrows():

        start_block = int(bin_row["start_block"])
        end_block   = int(bin_row["end_block"])
        vwap        = bin_row["vwap"]

        swaps_bin = swaps[
            (swaps["block"] >= start_block) &
            (swaps["block"] <= end_block)
        ]

        if swaps_bin.empty:
            continue

        tol_row = tolerance[
            tolerance["start_block"] == start_block
        ]

        if tol_row.empty:
            continue

        tolerance_value = tol_row.iloc[0]["tolerance"]

        # ----------------------
        # Build users
        # ----------------------
        users = build_users_historical(swaps_bin, tolerance_value)

        # ----------------------
        # Get liquidity snapshot at bin start
        # ----------------------
        liq_row = liquidity[
            liquidity["block"] == start_block
        ]

        if liq_row.empty:
            continue

        liq_row = liq_row.iloc[0]

        capital_total = compute_total_capital(liq_row)

        # ----------------------
        # Build LPs
        # ----------------------
        from dataclasses import asdict
        lps_raw = generate_lps(
            total_capital_usd=capital_total,
            n_lps=len(users),
            seed=RANDOM_SEED
        )
        lps = [asdict(lp) for lp in lps_raw]

        # ----------------------
        # Price grid centered around VWAP
        # ----------------------
        price_grid = np.linspace(
            vwap * (1 - PRICE_BAND),
            vwap * (1 + PRICE_BAND),
            7
        ).tolist()

        # ----------------------
        # FEE GRID DECISION
        # ----------------------
        if FEE_MATCH_UNISWAP:
                fee_grid = [FEE_UNISWAP]
        else:
                fee_grid = FEE_GRID

        # ----------------------
        # Historical baseline metrics
        # ----------------------
        uni_volume_eth = swaps_bin["eth_amount"].abs().sum()
        uni_volume_usd = uni_volume_eth * vwap

        instance = {
            "bin_id": start_block,
            "users": users,
            "lps": lps,
            "price_grid": price_grid,
            "fee_grid": fee_grid,
            "opts": {
                "binSeconds": BIN_SECONDS,
                "secondsPerYear": SECONDS_PER_YEAR
            },
            "historical": {
                "uni_volume_eth": uni_volume_eth,
                "uni_volume_usd": uni_volume_usd,
                "uni_price": vwap,
                "uni_liquidity_usd": capital_total
            }
        }

        instances.append(instance)

        logger.info(
            f"Built bin {start_block} | "
            f"Users={len(users)} | "
            f"Liquidity={capital_total:,.0f}"
        )

    return instances