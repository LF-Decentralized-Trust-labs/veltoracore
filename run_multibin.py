#!/usr/bin/env python3
import json
import csv
import random
import subprocess
from copy import deepcopy
import math
import pandas as pd

from instances import (
    # builders
    generate_users,
    generate_lps,
    price_grid_from_users,
)

from run_uniswap_bin import run_uniswap_bin

from builder_historical_solver import build_historical_instances

from config import *

# --------------------------------------------------
# Helper: initial reserves from capital
# --------------------------------------------------
def initial_reserves(total_cap_usd, P0):
    y0 = total_cap_usd / 2
    x0 = y0 / P0
    return x0, y0

# --------------------------------------------------
# CARRY-1 helpers: persistent per-LP inventory and mark-to-market value
# --------------------------------------------------
def seed_inventory(lps, P):
    """Split each LP's USD capital 50/50 at price P, once, at the first bin."""
    return [dict(lp, x=float(lp["capitalUSD"]) / 2.0 / P, y=float(lp["capitalUSD"]) / 2.0)
            for lp in lps]

def pool_value(lps, P):
    return sum(float(lp["x"]) * P + float(lp["y"]) for lp in lps)

# --------------------------------------------------
# Exogenous Market Price Generator
# --------------------------------------------------

def next_market_price(
    P_prev: float,
    annual_drift: float,
    annual_vol: float,
    bin_seconds: int,
    seconds_per_year: int
) -> float:
    """
    Geometric Brownian Motion with drift.

    dP / P = mu dt + sigma dW
    """

    dt = bin_seconds / seconds_per_year

    sigma = annual_vol
    mu = annual_drift

    z = random.gauss(0.0, 1.0)

    growth_factor = math.exp(
        (mu - 0.5 * sigma**2) * dt +
        sigma * math.sqrt(dt) * z
    )

    P_next = P_prev * growth_factor

    return max(P_next, 0.01)


# --------------------------------------------------
# Run VeltoraCore single bin via solver subprocess
# --------------------------------------------------
def run_single_bin_veltoracore(inst):
    proc = subprocess.run(
        ["python3", "solver.py"],
        input=json.dumps(inst),
        text=True,
        capture_output=True
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)

    return json.loads(proc.stdout)

# --------------------------------------------------
# Main multibin simulation
# --------------------------------------------------
def main():
    random.seed(RANDOM_SEED)

    rows = [] 

    ## BLOCK FOR STOCHASTIC PRICE
    ## STOCHASTIC PRICE DOES NOT RETRIEVE HISTORICAL DATA
    if PRICE_STOCHASTIC:
        # ------------------------------
        # Liquidity setup (Bin 0)
        # ------------------------------
        total_capital = LIQUIDITY_USD

        lps = generate_lps(
            total_capital_usd=total_capital,
            n_lps=N_LPS,
            seed=RANDOM_SEED
        )

        # MAIN INITIAL VARIABLES
        P0_uni= INITIAL_PRICE
        P0_opt= INITIAL_PRICE
        P_Market = INITIAL_PRICE
        x_uni, y_uni = initial_reserves(total_capital, P0_uni)

        cum_log_uni = 0
        cum_log_opt = 0

        # ------------------------------
        # Bin loop
        # ------------------------------
        for bin_id in range(N_BINS):

            print(f"\n=== BIN {bin_id:02d} ===")

            users = generate_users(
            n_users=N_USERS,
            price_center=P_Market,
            seed=RANDOM_SEED + bin_id
            )

            price_grid = [P_Market]
        
            if FEE_MATCH_UNISWAP:
                fee_grid = [FEE_UNISWAP]
            else:
                fee_grid = FEE_GRID
            # --------------------------
            # Run Uniswap V2
            # --------------------------
            sol_uni = run_uniswap_bin(
            x0=x_uni,
            y0=y_uni,
            users=[u.__dict__ for u in users],  
            fee=FEE_UNISWAP, 
            seed=RANDOM_SEED + bin_id,
            P0=P0_uni,
            P_market=P_Market
            )
        
            if CARRY_INVENTORY and bin_id == 0:
                lps = seed_inventory([lp.__dict__ for lp in lps], P0_opt)   # CARRY-1
            inst = {
                "bin_id": bin_id,
                "p0": P0_opt,
                "users": [u.__dict__ for u in users],
                "lps": lps if CARRY_INVENTORY else [lp.__dict__ for lp in lps],
                "price_grid": price_grid,
                "fee_grid": fee_grid,
                "opts": {
                    "binSeconds": BIN_SECONDS,
                    "secondsPerYear": SECONDS_PER_YEAR,
                },
            }

            # --------------------------
            # Run VeltoraCore
            # --------------------------
            sol_opt = run_single_bin_veltoracore(inst)

            if sol_opt["status"] != "OPTIMAL":
                print("❌ VeltoraCore infeasible")
                break

            ## COMPUTING CUMULATIVE LOG RETURNS
            cum_log_uni += math.log (1+sol_uni["return_LP"])
            cum_log_opt += math.log (1+sol_opt["lp_net"])

            # --------------------------
            # Record results
            # --------------------------
            rows.append({
                "bin": bin_id,
                "liquidity_usd": total_capital,
                "opt_capital_kept": sol_opt["total_cap_active_lps"],
                "user_demand_usd": TOTAL_USER_DEMAND_USD,
                "OK_user_demand_usd_inflation": sol_opt["total_users_original_demand_usd"],
                # the original demand is corrected by the inflation factor to be comparable 
                "opt_user_kept": sol_opt["kept_volume_P0"],
                "uni_user_kept": sol_uni["executed_volume_usd"],
                "opt_price": sol_opt["P"],
                "uni_price": sol_uni["P_end"],
                "check_price": P_Market,
                "opt_fee": sol_opt["fee"],
                "uni_fee": FEE_UNISWAP,
                "opt_lp_surplus": sol_opt["lp_surplus_volume"],
                "opt_protocol_objective": sol_opt["protocol_objective"],
                "opt_total_income": sol_opt["lp_fee_usd"],
                "uni_total_income": sol_uni["fee_income_usd"],
                "opt_il_usd": sol_opt["IL_USD"],
                "uni_il_usd": sol_uni["IL_USD"],
                "opt_net_income": sol_opt["lp_surplus_income"],
                "uni_net_income": sol_uni["fee_income_usd"] + sol_uni["IL_USD"],
                "opt_lp_return_bin": sol_opt["lp_net"], 
                "uni_lp_return_bin": sol_uni["return_LP"], 
                "uni_lp_return_bin_check": sol_uni["check_return"],
                "opt_log_cum_return_bin": cum_log_opt,
                "uni_log_cum_return_bin": cum_log_uni,
                "opt_lp_return_year": sol_opt["lp_net"] * SECONDS_PER_YEAR/BIN_SECONDS if BIN_SECONDS > 0 else 0.0, 
                "uni_lp_return_year": sol_uni["return_LP"] * SECONDS_PER_YEAR/BIN_SECONDS if BIN_SECONDS > 0 else 0.0, 
                "opt_solve_time_ms": sol_opt["solve_time_ms"],
                "opt_total_sweep_time_ms": sol_opt["total_sweep_time_ms"],
                "check_opt_return": sol_opt["return_check"]* SECONDS_PER_YEAR/BIN_SECONDS if BIN_SECONDS > 0 else 0.0,
            })

            # --------------------------
            # State update
            # --------------------------
            x_uni = sol_uni["x1"]
            y_uni = sol_uni["y1"]
            if CARRY_INVENTORY and sol_opt.get("lps_out") is not None:
                lps = sol_opt["lps_out"]                                   # CARRY-1
            
            #P0_opt = P_Market
            #P0_uni = P_Market
            P0_uni = sol_uni["P_end"]
            P0_opt = sol_opt["P"]
            P_Market = next_market_price(
            P_prev=P_Market,
            annual_drift=ANNUAL_DRIFT,
            annual_vol=ANNUAL_VOL,
            bin_seconds=BIN_SECONDS,
            seconds_per_year=SECONDS_PER_YEAR
            )
            # if price not stochastic
               # P0_uni = sol_uni["P_end"]
               # P0_opt = sol_opt["P"]

   
    ## HISTORICAL MODE COMPARING TO EXECUTED VOLUMES IN UNISWAP
    ## HISTORICAL MODE COMPARING TO EXECUTED VOLUMES IN UNISWAP
    elif RUN_HISTORICAL:

        # --------------------------------------------------
        # Load Uniswap historical per-bin RAW returns
        # --------------------------------------------------
        uni_df = pd.read_parquet("historical_data/uniswap_bins.parquet")
        uni_df = uni_df.set_index("start_block")

        instances = build_historical_instances()

        if not instances:
            raise ValueError("No historical instances built.")

        # --------------------------------------------------
        # Initial states
        # --------------------------------------------------
        P0_opt = instances[0]["historical"]["uni_price"]

        cum_log_opt = 0.0
        cum_log_uni = 0.0

        skipped_bins = 0
        valid_compounded_bins = 0

        carried_lps = None      # CARRY-1: one persistent LP set for the whole run
        V_prev = None
        cum_log_opt_mtm = 0.0

        # --------------------------------------------------
        # Historical bin loop
        # --------------------------------------------------
        for i, inst in enumerate(instances):

            bin_key = inst["bin_id"]
            print(f"\n=== HIST BIN {bin_key} ===")

            # ---------------------------------
            # Retrieve Uniswap per-bin return
            # ---------------------------------
            if bin_key not in uni_df.index:
                print(f"⚠ Missing Uniswap data for bin {bin_key}")
                continue

            uni_row = uni_df.loc[bin_key]
            uni_lp_return = uni_row["uni_lp_return_raw"]

            # ---------------------------------
            # Liquidity drift filter (skip only if i > 0)
            # ---------------------------------
            V0 = inst["historical"]["uni_liquidity_usd"]
            P0 = inst["historical"]["uni_price"]

            if i + 1 < len(instances):
                V1 = instances[i + 1]["historical"]["uni_liquidity_usd"]
                P1 = instances[i + 1]["historical"]["uni_price"]
            else:
                V1 = V0
                P1 = P0

            liq_drift = abs(V1 - V0) / V0 if V0 > 0 else 0.0
            price_drift = abs(P1 - P0) / P0 if P0 > 0 else 0.0

            # Do NOT filter first bin (calibration bin)
            if i > 0:
                if liq_drift > max(LIQ_DRIFT_THRESHOLD, LIQ_PRICE_MULTIPLIER * price_drift):
                    print(
                        f"⚠ Skipping bin {bin_key} "
                        f"(liq_drift={liq_drift:.3%}, "
                        f"price_drift={price_drift:.3%})"
                    )
                    skipped_bins += 1
                    continue

            # ---------------------------------
            # Inject previous-bin VeltoraCore price
            # ---------------------------------
            inst["p0"] = P0_opt

            if CARRY_INVENTORY:
                if carried_lps is None:
                    carried_lps = seed_inventory(inst["lps"], P0_opt)
                    V_prev = pool_value(carried_lps, P0_opt)
                inst["lps"] = carried_lps

            sol_opt = run_single_bin_veltoracore(inst)

            if sol_opt["status"] != "OPTIMAL":
                print("❌ VeltoraCore infeasible")
                break

            if sol_opt["total_cap_active_lps"] <= 0:
                print("❌ All LPs dropped — stopping historical run.")
                break

            lp_return_bin = sol_opt["lp_net"]

            # METRIC-1: mark-to-market LP return, same basis as uni_lp_return_raw
            # (value at this bin's price vs value at the previous bin's price).
            opt_return_mtm = None
            if CARRY_INVENTORY and sol_opt.get("lps_out") is not None:
                carried_lps = sol_opt["lps_out"]
                V_now = pool_value(carried_lps, sol_opt["P"])
                opt_return_mtm = V_now / V_prev - 1 if V_prev > 0 else 0.0
                V_prev = V_now
                if i > 0 and opt_return_mtm > -1:
                    cum_log_opt_mtm += math.log(1 + opt_return_mtm)

            # ---------------------------------
            # Manual cumulative compounding
            # Skip first bin for compounding
            # ---------------------------------
            if i > 0:

                if lp_return_bin > -1:
                    cum_log_opt += math.log(1 + lp_return_bin)

                if uni_lp_return > -1:
                    cum_log_uni += math.log(1 + uni_lp_return)

                valid_compounded_bins += 1

            # ---------------------------------
            # Record results
            # ---------------------------------
            rows.append({

                # Identification
                "bin": bin_key,

                # Liquidity
                "liquidity_usd": V0,
                "opt_capital_kept": sol_opt["total_cap_active_lps"],

                # Volume (ETH)
                "uni_volume_ether": inst["historical"]["uni_volume_eth"],
                "opt_user_kept_ether": sol_opt["kept_volume"],

                # Volume (USD)
                "uni_volume_usd": inst["historical"]["uni_volume_usd"],
                "opt_user_kept": sol_opt["kept_volume_P"],

                # Price
                "opt_price": sol_opt["P"],
                "uni_price": P0,
                "opt_fee": sol_opt["fee"],

                # Per-bin LP return
                "opt_lp_return_bin": lp_return_bin,
                "opt_lp_return_bin_mtm": opt_return_mtm,
                "opt_log_cum_return_mtm": cum_log_opt_mtm,
                "opt_x": sum(float(l["x"]) for l in carried_lps) if carried_lps else None,
                "opt_y": sum(float(l["y"]) for l in carried_lps) if carried_lps else None,
                "uni_lp_return_bin": uni_lp_return,

                # Cumulative log returns
                "opt_log_cum_return": cum_log_opt,
                "uni_log_cum_return": cum_log_uni,

                # Accounting
                "opt_total_income": sol_opt["lp_fee_usd"],
                "opt_il_usd": sol_opt["IL_USD"],
                "opt_net_income": sol_opt["lp_surplus_income"],
                "opt_lp_surplus_volume": sol_opt["lp_surplus_volume"],
                "opt_protocol_objective": sol_opt["protocol_objective"],

                # Diagnostics
                "opt_lp_return_year": (
                    lp_return_bin * SECONDS_PER_YEAR / BIN_SECONDS
                    if BIN_SECONDS > 0 else 0.0
                ),
                "check_opt_return": (
                    sol_opt["return_check"] * SECONDS_PER_YEAR / BIN_SECONDS
                    if BIN_SECONDS > 0 else 0.0
                ),
                "opt_solve_time_ms": sol_opt["solve_time_ms"],
                "opt_total_sweep_time_ms": sol_opt.get("total_sweep_time_ms", 0.0),
            })

            # ---------------------------------
            # Update VeltoraCore state
            # ---------------------------------
            P0_opt = sol_opt["P"]

        print("\n--------------------------------------")
        print(f"⚙ Skipped bins due to liquidity drift: {skipped_bins}")
        print(f"📊 Valid compounded bins: {valid_compounded_bins}")
        print("--------------------------------------\n")
        
    # ------------------------------
    # Write CSV
    # ------------------------------
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✅ Results written to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
