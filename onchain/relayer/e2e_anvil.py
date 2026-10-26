"""E2E on a local chain (anvil): contracts -> intents -> relayer -> VeltoraCore
engine (endogenous price) -> verifier -> claims.

    anvil --port 8545 &            # Foundry
    forge build                    # in onchain/
    python3 relayer/e2e_anvil.py --veltoracore /path/to/veltoracore
"""
import argparse, json, os, sys, time
from web3 import Web3
from eth_abi import encode
from eth_utils import keccak

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from relayer import Chain, Relayer, VeltoraCoreAdapter, KIND_LP_COMMIT, KIND_USER_SWAP, KIND_LP_WITHDRAW

ap = argparse.ArgumentParser()
ap.add_argument("--veltoracore", required=True)
ap.add_argument("--rpc", default="http://127.0.0.1:8545")
args = ap.parse_args()

w3 = Web3(Web3.HTTPProvider(args.rpc))
acct = w3.eth.accounts
DEPLOYER, OPERATOR, LP, ALICE, BOB, CAROL = acct[:6]
OUT = os.path.join(HERE, "..", "out")


def art(name):
    j = json.load(open(os.path.join(OUT, f"{name}.sol", f"{name}.json")))
    return j["abi"], j["bytecode"]["object"]


def deploy(name, *ctor):
    abi, bc = art(name)
    c = w3.eth.contract(abi=abi, bytecode=bc)
    rc = w3.eth.wait_for_transaction_receipt(c.constructor(*ctor).transact({"from": DEPLOYER}))
    return w3.eth.contract(address=rc.contractAddress, abi=abi)


def tx(fn, frm):
    return w3.eth.wait_for_transaction_receipt(fn.transact({"from": frm}))


def advance(seconds):
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])


eurc = deploy("MockStablecoin", "Test Euro Coin", "tEURC", 6)
usdc = deploy("MockStablecoin", "Test USD Coin", "tUSDC", 6)
gw = deploy("VeltoraGateway")
ver = deploy("VeltoraSettlementVerifier", gw.address)
MKT = keccak(text="veltoracore.eurc-usdc.anvil")
genesis = w3.eth.get_block("latest")["timestamp"]
tx(gw.functions.createMarket(MKT, OPERATOR, ver.address, genesis, 60, [eurc.address, usdc.address]), DEPLOYER)

for who in (LP, ALICE, BOB, CAROL):
    for t in (eurc, usdc):
        tx(t.functions.mint(who, 10 ** 13), DEPLOYER)
        tx(t.functions.approve(gw.address, 2 ** 256 - 1), who)

nonce = {}
def submit(who, kind, schema, assets, amounts, terms=b"", params=b"", expiry=10 ** 6):
    nonce[who] = nonce.get(who, 0) + 1
    it = (MKT, kind, keccak(text=schema), nonce[who], expiry, assets, amounts, terms, params)
    rc = tx(gw.functions.submit(it), who)
    ev = gw.events.IntentSubmitted().process_receipt(rc)[0]["args"]
    return ev["intentId"], ev["bin"]

def swap_terms(recv, pmin_human, send_dec, recv_dec):
    # pMinX18 is recv_raw per send_raw, scaled 1e18
    p = int(pmin_human * 10 ** recv_dec * 10 ** 18 // 10 ** send_dec)
    return encode(["address", "uint256", "bytes32", "bytes32"], [recv, p, b"\0" * 32, b"\0" * 32])

chain = Chain(w3, gw, ver, MKT, OPERATOR)
adapter = VeltoraCoreAdapter(solver_dir=args.veltoracore, x_asset=eurc.address, y_asset=usdc.address,
                             x_dec=6, y_dec=6, fee_grid=[0.0005, 0.001, 0.002, 0.003])
rel = Relayer(chain, adapter)
report = {}

# ---- bin 0: LP genesis. Endogenous reference price = pool ratio 1,080,000 / 1,000,000 = 1.08
pid, b0 = submit(LP, KIND_LP_COMMIT, "veltoracore.lp/1", [eurc.address, usdc.address],
                 [1_000_000 * 10 ** 6, 1_080_000 * 10 ** 6], params=b'{"floorAPR":0.0}')
advance(61)
s, P, fee, rc = rel.settle_bin(b0, w3.eth.block_number)
report["bin0"] = {"outcomes": len(s["outcomes"]), "gas": rc.gasUsed}

# ---- bin 1: alice buys EURC (max 1.09 USDC/EURC), bob sells EURC (min 1.07),
#             carol sells EURC but demands 1.20 (must stay open)
a_id, b1 = submit(ALICE, KIND_USER_SWAP, "veltoracore.user/1", [usdc.address], [10_800 * 10 ** 6],
                  terms=swap_terms(eurc.address, 1 / 1.09, 6, 6))
b_id, _ = submit(BOB, KIND_USER_SWAP, "veltoracore.user/1", [eurc.address], [5_000 * 10 ** 6],
                 terms=swap_terms(usdc.address, 1.07, 6, 6))
c_id, _ = submit(CAROL, KIND_USER_SWAP, "veltoracore.user/1", [eurc.address], [1_000 * 10 ** 6],
                 terms=swap_terms(usdc.address, 1.20, 6, 6))
advance(61)
s, P, fee, rc = rel.settle_bin(b1, w3.eth.block_number)
report["bin1"] = {"price": P, "fee": fee, "fills": len(s["fills"]), "deltas": len(s["deltas"]), "gas": rc.gasUsed}

st = {n: gw.functions.intent(i).call()[3] for n, i in (("alice", a_id), ("bob", b_id), ("carol", c_id))}
claims = {"alice_EURC": gw.functions.claimable(ALICE, eurc.address).call() / 1e6,
          "alice_USDC_refund": gw.functions.claimable(ALICE, usdc.address).call() / 1e6,
          "bob_USDC": gw.functions.claimable(BOB, usdc.address).call() / 1e6}
pos = {"EURC": gw.functions.positionBalance(pid, eurc.address).call(),
       "USDC": gw.functions.positionBalance(pid, usdc.address).call()}
pool = {"EURC": gw.functions.poolBalance(MKT, eurc.address).call(),
        "USDC": gw.functions.poolBalance(MKT, usdc.address).call()}

# ---- bin 3: LP withdraws; relayer exits the position (gap bin 2 closed by the same settlement)
advance(61)
w_id, b3 = submit(LP, KIND_LP_WITHDRAW, "veltoracore.lp/1", [], [], terms=encode(["bytes32"], [pid]))
advance(61)
s3, _, _, rc3 = rel.settle_bin(b3, w3.eth.block_number)
lp_back = {"EURC": gw.functions.claimable(LP, eurc.address).call() / 1e6,
           "USDC": gw.functions.claimable(LP, usdc.address).call() / 1e6}

# ---- solvency: the gateway holds exactly what it owes (escrow of carol still inside)
held = {"EURC": eurc.functions.balanceOf(gw.address).call(), "USDC": usdc.functions.balanceOf(gw.address).call()}
owed_eurc = sum(gw.functions.claimable(a, eurc.address).call() for a in (LP, ALICE, BOB, CAROL)) \
    + gw.functions.poolBalance(MKT, eurc.address).call() + 1_000 * 10 ** 6   # carol's open escrow
owed_usdc = sum(gw.functions.claimable(a, usdc.address).call() for a in (LP, ALICE, BOB, CAROL)) \
    + gw.functions.poolBalance(MKT, usdc.address).call()

report.update(status=st, claims=claims, lp_position_after_bin1=pos, pool_after_bin1=pool,
              partition_ok=(pos == pool), lp_exit=lp_back, bin3_gas=rc3.gasUsed,
              solvent=(held["EURC"] == owed_eurc and held["USDC"] == owed_usdc))
print(json.dumps(report, indent=1, default=str))
assert st == {"alice": 2, "bob": 2, "carol": 1}, st          # FILLED, FILLED, OPEN
assert report["partition_ok"] and report["solvent"]
print("E2E PASS")
