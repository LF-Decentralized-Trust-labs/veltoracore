"""Pins for JCB-SIMPLE-1: the sweep winner is the Joint Clearing Bargain score
(Videira 2026, SSRN 7107418, eqs. 1 and 3). Run: python3 -m pytest tests -q"""
import os, sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import solver

OPTS = {"binSeconds": 600, "secondsPerYear": 31536000}


def test_q_star_matches_preprint_figure_1():
    for p, q in [(0.25, 40.16), (0.50, 12.38), (0.60, 9.38), (0.76, 6.77), (1.00, 5.00)]:
        assert solver.jcb_q(p) == pytest.approx(q, abs=0.005)


def test_score_boundary_properties():
    p = 0.76
    for t in (0.1, 0.5, 1.0):                                   # diagonal slice z(t,t) = t^p
        assert solver.jcb_score(t, t, p) == pytest.approx(t ** p)
    assert solver.jcb_score(0.0, 0.8, p) == 0.0                 # a side at zero scores zero
    assert solver.jcb_score(0.8, 0.0, p) == 0.0
    assert solver.jcb_score(0.3, 0.9, p) == pytest.approx(solver.jcb_score(0.9, 0.3, p))
    assert solver.jcb_score(0.6, 0.6, p) > solver.jcb_score(1.0, 0.2, p)   # balance beats lopsided


def test_select_prefers_balanced_over_extremes():
    c = [{"lp_excess_p0": 10.0, "kept_volume_P0": 0.0},        # providers best, users at zero
         {"lp_excess_p0": 6.0, "kept_volume_P0": 6.0},         # balanced
         {"lp_excess_p0": 0.0, "kept_volume_P0": 10.0}]        # users best, providers at zero
    w = solver.jcb_select(c, 0.76)
    assert w is c[1] and w["jcb_candidates"] == 3


def test_select_constant_attainment_does_not_discriminate():
    c = [{"lp_excess_p0": 1.0, "kept_volume_P0": 5.0},
         {"lp_excess_p0": 3.0, "kept_volume_P0": 5.0}]
    assert solver.jcb_select(c, 0.60) is c[1]                   # flow equal -> decided by providers


def _bin():
    # buyers whose limits fall across the grid: raising the price loses users
    users = [{"q": 1.0, "p_min": None, "p_max": pm} for pm in (2001, 2003, 2005, 2007, 2009)]
    users += [{"q": -1.0, "p_min": 1990.0, "p_max": None}]
    lps = [{"x": 500.0, "y": 1e6, "floorAPR": 0.0}]
    return {"users": users, "lps": lps, "p0": 2000.0, "opts": OPTS,
            "price_grid": [1998.0, 2000.0, 2002.0, 2004.0, 2006.0, 2008.0],
            "fee_grid": [0.0005, 0.001, 0.003]}


def test_legacy_objective_unchanged():
    inst = _bin()
    w = solver.sweep(inst, objective="legacy")
    best = float("-inf")
    for fee in inst["fee_grid"]:
        for P in inst["price_grid"]:
            s = solver.solve_candidate(dict(inst, fee=fee, price=P), fee, None, OPTS)
            if s is not None:
                best = max(best, s["protocol_objective"])
    assert w["protocol_objective"] == pytest.approx(best)
    assert "jcb_score" not in w


def test_jcb_winner_is_the_argmax_and_uses_p_price_on():
    w = solver.sweep(_bin(), objective="jcb")
    assert w["jcb_p"] == 0.76 and w["jcb_q"] == pytest.approx(6.77, abs=0.005)
    assert 0.0 < w["jcb_x"] <= 1.0 and 0.0 < w["jcb_y"] <= 1.0
    one_price = dict(_bin(), price_grid=[2000.0])
    assert solver.sweep(one_price, objective="jcb")["jcb_p"] == 0.60   # fee-only sweep
