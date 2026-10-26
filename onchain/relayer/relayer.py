"""VeltoraCore relayer — reads the standard IntentSubmitted log, clears each closed
60 s bin through an engine adapter, and submits the settlement to the verifier.

Core + engine adapter:
  * VeltoraCoreAdapter  — the open 2-currency engine (x = risky/foreign asset,
                          y = USD leg). Endogenous price: genesis reference is
                          the pool's own deposit ratio, afterwards a grid around
                          the previous clearing price. No external oracle.

Invariants the relayer enforces BEFORE submitting (so the verifier never has to
reject an honest engine): integer limit check identical to the contract's,
exact per-asset conservation (rounding residue to the largest position), no
position below zero, deltas sorted by (positionId, asset).
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field

from eth_utils import keccak

KIND_LP_COMMIT, KIND_LP_WITHDRAW, KIND_USER_SWAP = 1, 3, 4      # wire code 2 is reserved (unused)
X18 = 10 ** 18


def ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


# --------------------------------------------------------------------------- #
# Chain access
# --------------------------------------------------------------------------- #
class Chain:
    def __init__(self, w3, gateway, verifier, market_id: bytes, operator: str):
        self.w3, self.gw, self.ver, self.mkt, self.op = w3, gateway, verifier, market_id, operator
        (_, _, _, self.genesis, self.bin_seconds, _, self.assets) = gateway.functions.market(market_id).call()

    def intents_upto_block(self, from_block: int, to_block: int):
        import warnings
        ev = self.gw.events.IntentSubmitted
        with warnings.catch_warnings():               # token Transfer logs in the same txs
            warnings.simplefilter("ignore")           # are not ours; web3 discards them
            logs = ev.get_logs(from_block=from_block, to_block=to_block,
                               argument_filters={"marketId": self.mkt})
        out = []
        for lg in logs:
            a = lg["args"]; it = a["intent"]
            out.append(dict(intentId=a["intentId"], owner=a["owner"], seq=a["seq"], bin=a["bin"],
                            kind=it["kind"], assets=list(it["assets"]), amounts=list(it["amounts"]),
                            terms=it["terms"], params=it["params"], expiryBin=it["expiryBin"]))
        return sorted(out, key=lambda r: r["seq"])

    def position_balance(self, pid: bytes, asset: str) -> int:
        return self.gw.functions.positionBalance(pid, asset).call()

    def status(self, iid: bytes) -> int:
        return self.gw.functions.intent(iid).call()[3]

    def submit_settlement(self, s: dict):
        tup = (s["bin"], s["coveredCount"], s["coveredHash"], s["outputHash"], s["engineId"],
               [(o["intentId"], o["accepted"]) for o in s["outcomes"]],
               [(f["intentId"], f["sendUsed"], f["recvAmount"]) for f in s["fills"]],
               [(d["positionId"], d["asset"], d["delta"]) for d in s["deltas"]],
               s["exits"])
        tx = self.ver.functions.settle(self.mkt, tup).transact({"from": self.op})
        return self.w3.eth.wait_for_transaction_receipt(tx)


# --------------------------------------------------------------------------- #
# VeltoraCore adapter (2-currency, endogenous price)
# --------------------------------------------------------------------------- #
@dataclass
class VeltoraCoreAdapter:
    solver_dir: str                     # path to the VeltoraCore repo (solver.py)
    x_asset: str                        # risky / foreign leg (e.g. EURC)
    y_asset: str                        # USD leg (e.g. USDC)
    x_dec: int = 6
    y_dec: int = 6
    fee_grid: list = field(default_factory=lambda: [0.0001, 0.00025, 0.0005, 0.001, 0.002, 0.003])
    price_band: float = 0.003
    price_steps: int = 7
    bin_seconds: int = 60
    last_price: float | None = None     # endogenous state: previous clearing price

    def __post_init__(self):
        sys.path.insert(0, self.solver_dir)
        import solver as _s                  # the open engine, unmodified API
        self.solver = _s
        with open(os.path.join(self.solver_dir, "solver.py"), "rb") as fh:
            self.engine_id = keccak(fh.read())

    # ---- unit helpers
    def hx(self, raw): return raw / 10 ** self.x_dec
    def hy(self, raw): return raw / 10 ** self.y_dec

    def clear(self, lps: list[dict], swaps: list[dict]):
        """lps: [{pid, x_raw, y_raw, floorAPR}], swaps: [{intentId, send, amount, recv, pMin}]
        Returns (fills, lp_new_raw, price, fee, raw_solution)."""
        X = sum(self.hx(l["x_raw"]) for l in lps); Y = sum(self.hy(l["y_raw"]) for l in lps)
        if X <= 0 or Y <= 0:
            return [], None, self.last_price, None, None
        P0 = self.last_price if self.last_price else Y / X           # genesis: pool ratio
        users, idx = [], []
        for s in swaps:
            p_raw = s["pMin"] / X18                                     # recv_raw per send_raw
            if s["send"] == self.y_asset:                               # buys x with y
                p_h = p_raw * 10 ** (self.y_dec - self.x_dec)           # x per y (human)
                q = self.hy(s["amount"]) * p_h                          # recv target in x
                users.append({"q": q, "p_min": None, "p_max": 1.0 / p_h})
            else:                                                       # sells x for y
                p_h = p_raw * 10 ** (self.x_dec - self.y_dec)           # y per x (human)
                users.append({"q": -self.hx(s["amount"]), "p_min": p_h, "p_max": None})
            idx.append(s)
        lp_in = [{"capitalUSD": 0.0, "floorAPR": l.get("floorAPR", 0.0),
                  "x": self.hx(l["x_raw"]), "y": self.hy(l["y_raw"])} for l in lps]
        grid = [P0 * (1 - self.price_band + 2 * self.price_band * k / (self.price_steps - 1))
                for k in range(self.price_steps)]
        opts = {"binSeconds": self.bin_seconds, "secondsPerYear": 31536000}
        # JCB-SIMPLE-1: the engine's own sweep picks the winner (config.OBJECTIVE),
        # so the on-chain path and the replay use the same rule.
        best = self.solver.sweep({"users": users, "lps": lp_in, "p0": P0, "opts": opts,
                                  "price_grid": grid, "fee_grid": self.fee_grid})
        if best is None:
            return [], None, P0, None, None
        P, fee = best["P"], best["fee"]
        # re-solve the winner to read the per-user decisions
        inst = {"users": users, "lps": lp_in, "p0": P0, "price": P, "opts": opts}
        won = _solve_w(self.solver, inst, fee, best)
        fills = []
        for s, u, w in zip(idx, users, won):
            if not w:
                continue
            if u["q"] > 0:   # buyer: recv x = q, pays q*P*(1+fee) in y
                recv = int(math.floor(u["q"] * 10 ** self.x_dec))
                used = ceil_div(int(round(u["q"] * P * (1 + fee) * 10 ** 12)) * 10 ** self.y_dec, 10 ** 12)
            else:            # seller: gives |q| x, recv |q|*P*(1-fee) in y
                used = s["amount"]
                recv = int(math.floor(-u["q"] * P * (1 - fee) * 10 ** self.y_dec))
            used = min(used, s["amount"])
            if recv * X18 < ceil_div(used * s["pMin"], 1) or used == 0:   # contract's exact test
                continue                                                   # (never submit a reject)
            fills.append({"intentId": s["intentId"], "sendUsed": used, "recvAmount": recv,
                          "send": s["send"], "recv": s["recv"]})
        self.last_price = P
        return fills, best["lps_out"], P, fee, best


def _solve_w(solver_mod, inst, fee, sol):
    """Replicates solve_execution_once's model on the ACTIVE LP set and returns w.
    Kept here (relayer side) so solver.py needs no API change."""
    from ortools.linear_solver import pywraplp
    active = [lp for lp, m in zip(inst["lps"], sol["active_mask"]) if m]
    x0 = sum(lp["x"] for lp in active); y0 = sum(lp["y"] for lp in active)
    P = inst["price"]; s = pywraplp.Solver.CreateSolver("CBC")
    w, dx, dy = [], [], []
    for i, u in enumerate(inst["users"]):
        wi = s.BoolVar(f"w{i}"); q = u["q"]
        if q > 0 and u["p_max"] is not None and P * (1 + fee) > u["p_max"]: s.Add(wi == 0)
        if q < 0 and u["p_min"] is not None and P * (1 - fee) < u["p_min"]: s.Add(wi == 0)
        dx.append(-wi * q)
        dy.append(wi * q * P * ((1 + fee) if q > 0 else (1 - fee)))
        w.append(wi)
    s.Add(x0 + sum(dx) >= 0); s.Add(y0 + sum(dy) >= 0)
    obj = s.Objective()
    for i, u in enumerate(inst["users"]): obj.SetCoefficient(w[i], abs(u["q"]))
    obj.SetMaximization(); s.Solve()
    return [wi.solution_value() > 0.5 for wi in w]


# --------------------------------------------------------------------------- #
# Relayer loop body: one bin
# --------------------------------------------------------------------------- #
class Relayer:
    def __init__(self, chain: Chain, adapter: VeltoraCoreAdapter, state_path: str | None = None):
        self.c, self.a = chain, adapter
        self.positions: dict[bytes, dict] = {}     # pid -> {floorAPR}
        self.covered = 0
        self.from_block = 0

    def settle_bin(self, bin_no: int, to_block: int):
        new = self.c.intents_upto_block(self.from_block, to_block)
        new = [r for r in new if r["seq"] > self.covered and r["bin"] <= bin_no]
        covered = max([self.covered] + [r["seq"] for r in new])
        outcomes, exits = [], []
        for r in new:                                     # LP admissions: accept all valid
            if r["kind"] == KIND_LP_COMMIT:
                outcomes.append({"intentId": r["intentId"], "accepted": True})
                params = json.loads(r["params"] or b"{}")
                self.positions[r["intentId"]] = {"floorAPR": float(params.get("floorAPR", 0.0))}
            elif r["kind"] == KIND_LP_WITHDRAW:
                pid = r["terms"][:32]
                outcomes.append({"intentId": r["intentId"], "accepted": True})
                exits.append(pid)
        # inventory as the chain will hold it AFTER admissions (read + pending commits)
        pending = {r["intentId"]: r for r in new if r["kind"] == KIND_LP_COMMIT}
        lps = []
        for pid, meta in self.positions.items():
            if pid in exits:
                continue
            if pid in pending:
                amt = dict(zip(pending[pid]["assets"], pending[pid]["amounts"]))
                xr, yr = amt.get(self.a.x_asset, 0), amt.get(self.a.y_asset, 0)
            else:
                xr = self.c.position_balance(pid, self.a.x_asset)
                yr = self.c.position_balance(pid, self.a.y_asset)
            lps.append({"pid": pid, "x_raw": xr, "y_raw": yr, "floorAPR": meta["floorAPR"]})
        swaps = []
        open_swaps = [r for r in self._open_swaps(new) if r["expiryBin"] >= bin_no]
        for r in open_swaps:
            recv, pmin = _decode_swap_terms(r["terms"])
            swaps.append({"intentId": r["intentId"], "send": r["assets"][0], "amount": r["amounts"][0],
                          "recv": recv, "pMin": pmin})
        fills, lps_out, P, fee, raw = self.a.clear(lps, swaps) if (lps and swaps) else ([], None, self.a.last_price, None, None)
        deltas = self._deltas(lps, raw["active_mask"], fills) if fills else []
        for pid in exits:
            self.positions.pop(pid, None)
        s = {"bin": bin_no, "coveredCount": covered,
             "coveredHash": self.c.gw.functions.runningHashAt(self.c.mkt, covered).call(),
             "outputHash": keccak(json.dumps({"P": P, "fee": fee, "fills": [
                 [f["intentId"].hex(), f["sendUsed"], f["recvAmount"]] for f in fills]},
                 sort_keys=True).encode()),
             "engineId": self.a.engine_id, "outcomes": outcomes,
             "fills": [{k: f[k] for k in ("intentId", "sendUsed", "recvAmount")} for f in fills],
             "deltas": deltas, "exits": exits}
        rcpt = self.c.submit_settlement(s)
        self.covered = covered
        self._open_cache = [r for r in open_swaps if r["intentId"] not in {f["intentId"] for f in fills}]
        return s, P, fee, rcpt

    _open_cache: list = []

    def _open_swaps(self, new):
        return [r for r in self._open_cache] + [r for r in new if r["kind"] == KIND_USER_SWAP]

    def _deltas(self, lps, active_mask, fills):
        """Integer per-position deltas derived from the SUBMITTED fills (never from
        a separate solve): the users' net flow per asset is shared among ACTIVE
        positions in proportion to their pre-trade holding of that asset (the
        CARRY-1 rule). Per-asset sums equal the user net flow EXACTLY; the
        rounding residue goes to the largest active holder."""
        user_net = {self.a.x_asset: 0, self.a.y_asset: 0}
        for f in fills:
            user_net[f["send"]] += f["sendUsed"]; user_net[f["recv"]] -= f["recvAmount"]
        act = [i for i, m in enumerate(active_mask) if m]
        out = []
        for asset, raw_key in ((self.a.x_asset, "x_raw"), (self.a.y_asset, "y_raw")):
            pool = sum(lps[i][raw_key] for i in act)
            ds = [0] * len(lps)
            if pool > 0:
                for i in act:
                    ds[i] = (user_net[asset] * lps[i][raw_key]) // pool
                big = max(act, key=lambda i: lps[i][raw_key])
                ds[big] += user_net[asset] - sum(ds)
            assert all(l[raw_key] + d >= 0 for l, d in zip(lps, ds)), "negative position"
            out += [{"positionId": l["pid"], "asset": asset, "delta": d}
                    for l, d in zip(lps, ds) if d != 0]
        return sorted(out, key=lambda d: (d["positionId"], int(d["asset"], 16)))


def _decode_swap_terms(terms: bytes):
    from eth_abi import decode
    from web3 import Web3
    recv, pmin, _, _ = decode(["address", "uint256", "bytes32", "bytes32"], terms)
    return Web3.to_checksum_address(recv), pmin      # eth_abi returns lowercase
