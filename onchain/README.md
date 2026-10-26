# onchain/ — VeltoraCore intent standard (testnet)

Two-asset settlement layer for VeltoraCore: users and LPs post signed intents to a
gateway (escrow on-chain), the off-chain engine clears each 60 s bin with an
endogenous price (no oracle), and a verifier contract accepts the settlement only
if it passes the checks below. Anyone can re-run the deterministic engine on the
logged intents and compare the logged output hash.

Verifier checks: operator only · monotone bins · coverage (the engine attests it saw
exactly the logged intents; no censorship, no look-ahead) · limit (no fill below the
user's minimum, no overspend of escrow) · once (no double or expired fill) ·
conservation per asset (no value leak, no fee skim) · no position below zero ·
sorted, unique deltas.

Schemas: `veltoracore.lp/1` (params `{"floorAPR": x}`) and `veltoracore.user/1`.

## Setup
Dependencies (not vendored), under `lib/`:

    git clone --depth 1 https://github.com/foundry-rs/forge-std lib/forge-std
    git clone --depth 1 --branch v5.0.2 https://github.com/OpenZeppelin/openzeppelin-contracts lib/openzeppelin-contracts

Foundry and solc 0.8.26 (legacy pipeline, `via_ir = false`). Python: `pip install web3 eth-abi`
plus the repo's `requirements.txt`.

## Run

    forge test                                   # contract tests
    anvil &
    forge build && python3 relayer/e2e_anvil.py --veltoracore ..   # local end-to-end

## Testnet (Sepolia)

    export SEPOLIA_RPC=... PRIVATE_KEY=... OPERATOR=0x...
    forge script script/Deploy.s.sol --rpc-url $SEPOLIA_RPC --broadcast
