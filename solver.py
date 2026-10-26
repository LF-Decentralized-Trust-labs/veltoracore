#!/usr/bin/env python3
# ============================================================
# VeltoraCore Solver: every LP participates, pro rata
# (Price-sweep aware, market valuation at swept price)
# ============================================================

import json
import math
import sys
import time
from copy import deepcopy
from ortools.linear_solver import pywraplp


def sum_capital(lps):
    return sum(float(lp.get("capitalUSD", 0.0)) for lp in lps)

# ------------------------------------------------------------
# Single execution solve (conditional on swept price P)
# ------------------------------------------------------------

# Conventions:
#   x : base (risky) asset
#   y : USD / quote asset
#   P : USD per unit of x (y/x)
#   q (always on x) : user order size (signed x) - positive buy(negative for bin), negative sell (positive for bin)

def solve_execution_once(inst, fee, time_limit=None):
    users = inst.get("users", [])
    lps   = inst.get("lps", [])

    #fee = float(inst.get("fee", 0.0))

    # Prices must be provided by orchestrator
    P0 = float(inst["p0"])      # previous-bin market price
    P  = float(inst["price"])   # swept market price

    # --------------------------------------------------------
    # LP capital (USD)
    # --------------------------------------------------------
    total_cap = sum_capital(lps)
    if total_cap <= 0:
        return {"status": "INFEASIBLE", "reason": "no_lp_capital"}

    # CARRY-1: if every LP carries its own inventory (x, y), use it (operational
    # mode: real deposits cannot be rebalanced for free between bins). Otherwise
    # fall back to the legacy per-bin 50/50 restrike at the previous price.
    carried = all(("x" in lp and "y" in lp) for lp in lps)
    if carried:
        x0 = sum(float(lp["x"]) for lp in lps)
        y0 = sum(float(lp["y"]) for lp in lps)
    else:
        y0 = total_cap / 2.0                  # USD
        x0 = (total_cap / 2.0) / P0           # asset

    # --------------------------------------------------------
    # Solver
    # --------------------------------------------------------
    solver = pywraplp.Solver.CreateSolver("CBC")
    if solver is None:
        return {"status": "INFEASIBLE", "reason": "solver_init_failed"}

    if time_limit:
        solver.SetTimeLimit(int(float(time_limit) * 1000))  # TIMELIMIT-1 (seconds -> ms)

    w, dx, dy = [], [], []

    # --------------------------------------------------------
    # User execution constraints
    # --------------------------------------------------------
    for i, u in enumerate(users):
        wi  = solver.BoolVar(f"w_{i}")
        dxi = solver.NumVar(-solver.infinity(), solver.infinity(), f"dx_{i}")
        dyi = solver.NumVar(-solver.infinity(), solver.infinity(), f"dy_{i}")

        q = float(u.get("q", 0.0))   # signed: +buy x, -sell x
        p_min = u.get("p_min", None)
        p_max = u.get("p_max", None)

        # Enforce user price feasibility at swept price P
        # Buy (q>0): require P <= p_max
        # Sell (q<0): require P >= p_min
        P_buy  = P * (1 + fee)   # what a buyer pays per unit of x
        P_sell = P * (1 - fee)   # what a seller receives per unit of x (SELL-GATE-1)

        # Buy (q > 0): user pays P_buy, must be ≤ p_max
        if q > 0 and p_max is not None and P_buy > p_max:
            solver.Add(wi == 0)

        # Sell (q < 0): user receives P_sell, must be ≥ p_min
        if q < 0 and p_min is not None and P_sell < p_min:
            solver.Add(wi == 0)

        if q > 0:
        # LP-perspective inventory deltas
        # Buy x  (q>0): LP loses x, gains y
        # Sell x (q<0): LP gains x, loses y
            solver.Add(dxi == -wi * q)
            solver.Add(dyi ==  wi * q * P * (1 + fee))
        elif q < 0:
            solver.Add(dxi == -wi * q)
            solver.Add(dyi ==  wi * q * P * (1 - fee))

        w.append(wi)
        dx.append(dxi)
        dy.append(dyi)

    # LP inventory must remain non-negative
    # Block below is important because joins dx and dy to optimization regarding LP inventory
    solver.Add(x0 + solver.Sum(dx) >= 0)
    solver.Add(y0 + solver.Sum(dy) >= 0)

    # --------------------------------------------------------
    # Objective: maximize executed volume (absolute)
    # --------------------------------------------------------
    obj = solver.Objective()
    for i, u in enumerate(users):
        obj.SetCoefficient(w[i], abs(float(u.get("q", 0.0))))
    obj.SetMaximization()

    
    status = solver.Solve()
    if status != pywraplp.Solver.OPTIMAL:
        return {"status": "INFEASIBLE", "reason": "no_optimal_solution"}

    # --------------------------------------------------------
    # Post-execution accounting
    # --------------------------------------------------------
    kept_volume = sum(
        abs(float(users[i].get("q", 0.0)))
        for i in range(len(users))
        if w[i].solution_value() > 0.5
    )

    kept_users = [
    users[i]
    for i in range(len(users))
    if w[i].solution_value() > 0.5
    ]

    x1 = x0 + sum(dx[i].solution_value() for i in range(len(dx)))
    y1 = y0 + sum(dy[i].solution_value() for i in range(len(dy)))

    # Diagnostic effective ratio (not a market price)
    P_eff = y1 / x1 if x1 > 0 else 0.0

    # --- User executed value (USD)
    user_value_usd = kept_volume * P

    # --- LP fee income (USD)
    lp_fee_income_usd = user_value_usd * fee

     # Remove fees from y1 before computing IL
    wallet_now_no_fee = (y1 - lp_fee_income_usd) + x1 * P
    wallet_hold = y0 + x0 * P

    # IL <= 0 means loss to LP
    IL_USD = wallet_now_no_fee - wallet_hold
    IL_percent = ((wallet_now_no_fee - wallet_hold) / wallet_hold)*100 if wallet_hold > 0 else 0.0

    # --- LP surplus before floor (USD)
    # IL_USD is negative when LPs lose
    lp_surplus_usd = lp_fee_income_usd + IL_USD

    lp_surplus_volume = lp_surplus_usd / fee if fee > 0 else 0.0

    # --- Protocol objective (USD)
    # Protocol objective expressed in execution-volume-equivalent units:
    #   user USD value + (LP surplus USD normalized by fee)
    protocol_objective = user_value_usd + lp_surplus_volume

    # CARRY-1: allocate the pool's post-trade inventory back to the LPs,
    # per asset, proportionally to each LP's pre-trade holding of that asset.
    # Multiplicative, so no LP can go negative; exact pro-rata when all LPs
    # hold the same composition. Conservation: sum over LPs == (x1, y1).
    lps_out = None
    if carried:
        fx = (x1 / x0) if x0 > 0 else 0.0
        fy = (y1 / y0) if y0 > 0 else 0.0
        lps_out = [dict(lp, x=float(lp["x"]) * fx, y=float(lp["y"]) * fy) for lp in lps]

    return {
        "status": "OPTIMAL",
        "lps_out": lps_out,
        "total_users": len(users),
        "count_dropped_users": len([u for u in users if u not in kept_users]),
        "total_users_original_demand_usd": sum(abs(float(u.get("q", 0.0))) * P0 for u in users),
        "kept_volume_P0": kept_volume * P0,
        "kept_volume_P": user_value_usd,
        "kept_volume": kept_volume,
        "lp_fee_usd": lp_fee_income_usd,
        "IL_USD": IL_USD,
        "IL_percent": IL_percent,
        "lp_surplus_income": lp_surplus_usd,
        "lp_surplus_volume": lp_surplus_volume,
        "protocol_objective": protocol_objective,
        # JCB-SIMPLE-1: provider revenue above the do-nothing position, both valued
        # at the frozen previous-bin price P0 (same baseline for every candidate).
        "lp_excess_p0": (y1 + x1 * P0) - (y0 + x0 * P0),
        "x0": x0,
        "x1": x1,
        "y0": y0,
        "y1": y1,
        "P0": P0,
        "P": P,
        #"P_eff": P_eff,
        "fee": fee,
        "income_check": (y1 + x1 * P) - (y0 + x0 * P),
        #"active_users": kept_users
    }

# ------------------------------------------------------------
# One candidate = one solve, every LP in (LP-ALL-IN-1)
# ------------------------------------------------------------
def solve_candidate(inst, fee, time_limit=None, opts=None):
    """Solve one (fee, price) candidate with every LP participating, pro rata.
    An LP's floorAPR is reported, not enforced: `lps_below_floor` counts the LPs whose
    pro-rata net return this bin is below their floor (needs opts.binSeconds > 0)."""
    t0 = time.perf_counter()
    opts = opts or {}
    lps = deepcopy(inst.get("lps", []))
    if lps and all(("x" in lp and "y" in lp) for lp in lps):
        P0 = float(inst["p0"])                      # CARRY-1: capital = x*P0 + y
        for lp in lps:
            lp["capitalUSD"] = float(lp["x"]) * P0 + float(lp["y"])
    if not lps:
        return None

    sol = solve_execution_once(dict(inst, lps=lps), fee, time_limit)
    if sol is None or sol.get("status") != "OPTIMAL":   # CRASH-1
        return None

    total_cap = sum_capital(lps)
    surplus = sol["lp_surplus_income"]
    bin_seconds = float(opts.get("binSeconds", 0.0))
    seconds_per_year = float(opts.get("secondsPerYear", 31536000))
    below = None
    if bin_seconds > 0 and total_cap > 0:
        net_apr = (surplus / total_cap) * (seconds_per_year / bin_seconds)   # pro rata: the same for every LP
        below = sum(1 for lp in lps if net_apr < float(lp.get("floorAPR", 0.0)))

    sol["active_mask"] = [True] * len(lps)
    sol["total_count_lps"] = len(lps)
    sol["dropped_count_lps"] = 0
    sol["lps_below_floor"] = below
    sol["total_cap_original_lps"] = sum_capital(inst.get("lps", []))
    sol["total_cap_active_lps"] = total_cap
    sol["lp_net"] = surplus / total_cap if total_cap > 0 else 0.0
    sol["return_check"] = sol["income_check"] / total_cap if total_cap > 0 else 0.0
    sol["solve_time_ms"] = (time.perf_counter() - t0) * 1000.0
    return sol

# ------------------------------------------------------------
# JCB-SIMPLE-1: Joint Clearing Bargain winner selection
# Videira (2026), "The Joint Clearing Bargain", SSRN 7107418, section 2.
# ------------------------------------------------------------
def jcb_q(p):
    """Eq. (3): q = q*(p), the larger root of c q^2 - (c p + 2) q + p (1 - p) = 0,
    with c = p^2 / (1 + p)."""
    c = p * p / (1.0 + p)
    D = (c * p + 2.0) ** 2 - 4.0 * c * p * (1.0 - p)
    return ((c * p + 2.0) + math.sqrt(D)) / (2.0 * c)


def jcb_score(x, y, p, q=None):
    """Eq. (1): z = m^p * (1 - r^2)^q, m = (x + y)/2, r = (x - y)/(x + y), x, y in [0, 1]."""
    if q is None:
        q = jcb_q(p)
    if x + y <= 0.0:
        return 0.0
    m = (x + y) / 2.0
    r = (x - y) / (x + y)
    return (m ** p) * (max(0.0, 1.0 - r * r) ** q)


def jcb_select(cands, p):
    """Pick the candidate with the highest JCB score. Per bin, each attainment is
    mapped to [0, 1] by its worst and best value over the feasible candidates
    (Kalai-Smorodinsky normalization): providers x = lp_excess_p0, users y =
    kept_volume_P0 (executed flow at the frozen price). An attainment that is equal
    for every candidate cannot discriminate and is set to 1. Ties: first in sweep order."""
    if not cands:
        return None
    q = jcb_q(p)

    def norm(vals):
        lo, hi = min(vals), max(vals)
        if hi - lo <= 1e-12 * max(1.0, abs(hi)):
            return [1.0] * len(vals)
        return [(v - lo) / (hi - lo) for v in vals]

    xs = norm([float(s["lp_excess_p0"]) for s in cands])
    ys = norm([float(s["kept_volume_P0"]) for s in cands])
    best_i, best_z = 0, float("-inf")
    for i, (x, y) in enumerate(zip(xs, ys)):
        z = jcb_score(x, y, p, q)
        if z > best_z:
            best_i, best_z = i, z
    best = cands[best_i]
    best.update({"jcb_p": p, "jcb_q": q, "jcb_score": best_z,
                 "jcb_x": xs[best_i], "jcb_y": ys[best_i], "jcb_candidates": len(cands)})
    return best


def sweep(inst, objective=None):
    """Mode B: solve every (fee, price) candidate, return the winner (or None)."""
    if objective is None:
        from config import OBJECTIVE as objective
    time_limit = inst.get("timeLimit", None)
    opts       = inst.get("opts", {})
    price_grid = inst.get("price_grid") or [inst.get("price")]
    fee_grid   = inst.get("fee_grid") or [inst.get("fee", 0.0)]

    cands = []
    for fee in fee_grid:
        for P in price_grid:
            inst_try = deepcopy(inst)
            inst_try["fee"] = fee
            inst_try["price"] = P
            sol = solve_candidate(inst_try, fee, time_limit, opts)
            if sol is not None:
                cands.append(sol)

    if objective == "jcb":
        from config import JCB_P_PRICE_ON, JCB_P_PRICE_OFF
        p = JCB_P_PRICE_ON if len(set(price_grid)) > 1 else JCB_P_PRICE_OFF
        return jcb_select(cands, p)

    best_sol, best_obj = None, float("-inf")          # legacy: protocol_objective
    for sol in cands:
        obj = sol.get("protocol_objective", float("-inf"))
        if obj > best_obj:
            best_obj, best_sol = obj, sol
    return best_sol


def main():
    inst = json.load(sys.stdin)

    time_limit = inst.get("timeLimit", None)
    opts       = inst.get("opts", {})

    price_grid = inst.get("price_grid", None)
    fee_grid   = inst.get("fee_grid", None)

    # --------------------------------------------------
    # Mode A: single-shot (used by smoke tests)
    # --------------------------------------------------
    if not price_grid and not fee_grid:
        fee = float(inst.get("fee", 0.0))
        sol = solve_candidate(inst, fee, time_limit, opts)

        if sol is None:
            print(json.dumps({"status": "INFEASIBLE"}))
        else:
            sol["status"] = "OPTIMAL"
            print(json.dumps(sol))
        return

    # --------------------------------------------------
    # Mode B: sweep (used by orchestrator)
    # --------------------------------------------------
    t_sweep_start = time.perf_counter()
    best_sol = sweep(inst)                            # JCB-SIMPLE-1 (config.OBJECTIVE)
    total_sweep_time_ms = (time.perf_counter() - t_sweep_start) * 1000.0

    if best_sol is None:
        print(json.dumps({"status": "INFEASIBLE"}))
        return

    best_sol["status"] = "OPTIMAL"
    best_sol["total_sweep_time_ms"] = total_sweep_time_ms
    print(json.dumps(best_sol))

if __name__ == "__main__":
    main()
