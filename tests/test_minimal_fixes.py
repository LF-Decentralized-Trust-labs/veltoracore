"""Pins for the minimal operational fixes (DIRECTION-1, SELL-GATE-1, CARRY-1,
CRASH-1, TIMELIMIT-1). Run from the repo root:  python3 -m pytest tests -q"""
import os, sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import solver
from builder_historical_solver import build_users_historical

OPTS = {"binSeconds": 600, "secondsPerYear": 31536000}

def _inst(users, lps, P=2000.0, P0=2000.0):
    return {"users": users, "lps": lps, "p0": P0, "price": P, "opts": OPTS}

def test_direction_pool_perspective():
    df = pd.DataFrame([{"eth_amount": 1.0, "price": 2000.0, "direction": "buy"},    # pool bought -> user SOLD
                       {"eth_amount": 2.0, "price": 2000.0, "direction": "sell"}])  # pool sold  -> user BOUGHT
    u = build_users_historical(df, 0.01)
    assert u[0]["q"] == -1.0 and u[0]["p_min"] is not None and u[0]["p_max"] is None
    assert u[1]["q"] == 2.0 and u[1]["p_max"] is not None and u[1]["p_min"] is None

def test_sell_gate_uses_net_proceeds():
    # seller demands >= 1995; at P=2000, fee=0.5% she receives 1990 -> must NOT fill
    users = [{"q": -1.0, "p_min": 1995.0, "p_max": None}]
    lps = [{"capitalUSD": 1e7, "floorAPR": 0.0}]
    s = solver.solve_execution_once(_inst(users, lps), fee=0.005)
    assert s["kept_volume"] == 0.0
    # same seller at 1990 floor fills
    users[0]["p_min"] = 1990.0
    s = solver.solve_execution_once(_inst(users, lps), fee=0.005)
    assert s["kept_volume"] == 1.0

def test_fee_charged_on_whole_usd_leg_both_directions():
    lps = [{"capitalUSD": 1e7, "floorAPR": 0.0}]
    b = solver.solve_execution_once(_inst([{"q": 1.0, "p_min": None, "p_max": 1e9}], lps), fee=0.01)
    s = solver.solve_execution_once(_inst([{"q": -1.0, "p_min": 0.0, "p_max": None}], lps), fee=0.01)
    assert abs((b["y1"] - b["y0"]) - 2000.0 * 1.01) < 1e-6    # buyer pays P(1+fee)
    assert abs((s["y0"] - s["y1"]) - 2000.0 * 0.99) < 1e-6    # seller receives P(1-fee)

def test_carry_uses_lp_inventory_and_conserves():
    lps = [{"capitalUSD": 0, "floorAPR": 0.0, "x": 0.5, "y": 10_000.0},
           {"capitalUSD": 0, "floorAPR": 0.0, "x": 1.5, "y": 30_000.0}]
    users = [{"q": 1.0, "p_min": None, "p_max": 1e9}, {"q": 5.0, "p_min": None, "p_max": 1e9}]
    sol = solver.solve_candidate(_inst(users, lps), fee=0.001, opts=OPTS)
    # only 2 ETH in the carried book: the 5-ETH buyer cannot fill (restrike would have allowed it)
    assert sol["kept_volume"] == 1.0
    xs = [lp["x"] for lp in sol["lps_out"]]; ys = [lp["y"] for lp in sol["lps_out"]]
    assert abs(sum(xs) - sol["x1"]) < 1e-9 and abs(sum(ys) - sol["y1"]) < 1e-6
    assert min(xs) >= 0 and min(ys) >= 0
    assert "_i" not in sol["lps_out"][0]

def test_every_lp_participates_and_a_missed_floor_is_reported():
    # LP-ALL-IN-1: an unattainable floor no longer takes the LP out; it is counted in lps_below_floor
    lps = [{"capitalUSD": 0, "floorAPR": 0.0, "x": 1.0, "y": 2000.0},
           {"capitalUSD": 0, "floorAPR": 99.0, "x": 1.0, "y": 2000.0}]
    users = [{"q": 0.1, "p_min": None, "p_max": 1e9}]
    sol = solver.solve_candidate(_inst(users, lps), fee=0.003, opts=OPTS)
    assert sol["active_mask"] == [True, True]
    assert sol["dropped_count_lps"] == 0 and sol["lps_below_floor"] == 1
    assert sol["lps_out"][0]["x"] < 1.0 and sol["lps_out"][1]["x"] < 1.0      # both LPs took the trade, pro rata
    assert abs(sol["lps_out"][0]["x"] - sol["lps_out"][1]["x"]) < 1e-12

def test_legacy_restrike_unchanged_without_inventory():
    lps = [{"capitalUSD": 4000.0, "floorAPR": 0.0}]
    s = solver.solve_execution_once(_inst([], lps), fee=0.003)
    assert s["x0"] == 1.0 and s["y0"] == 2000.0 and s["lps_out"] is None

def test_crash_guard_zero_capital_returns_none():
    lps = [{"capitalUSD": 0.0, "floorAPR": 0.0}]
    assert solver.solve_candidate(_inst([{"q": 1.0, "p_min": None, "p_max": 1e9}], lps),
                                            fee=0.003, opts=OPTS) is None
