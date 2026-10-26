// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {VeltoraTypes as T} from "./VeltoraTypes.sol";
import {VeltoraGateway} from "./VeltoraGateway.sol";

/// @title VeltoraSettlementVerifier — on-chain feasibility check of a bin settlement.
/// @notice What this contract PROVES on every bin, before a single unit moves:
///   V1 operator    — only the market's engine key may settle.
///   V2 monotone    — bins settle in strictly increasing order.
///   V3 coverage    — the engine attests it saw EXACTLY the intents logged up to this
///                    bin (running hash match + boundary check): no silent censorship,
///                    no look-ahead into a later bin.
///   V4 once        — every intent is resolved at most once; only OPEN intents of the
///                    right kind, logged no later than this bin and not expired.
///   V5 limit       — every fill honours the user's signed limit: recv*1e18 >= sendUsed*pMin,
///                    and never uses more than the escrowed send amount.
///   V6 conservation— per market asset: sum(sendUsed) - sum(recv) == sum(position deltas).
///                    Value cannot be created or leaked; fees are only a re-division. Every fill
///                    amount must fit int256 (no wrap-around in the signed sums).
///   V7 solvency    — no LP position goes negative in any asset (gateway reverts).
///   V8 anchoring   — outputHash + engineId are logged, so anyone can re-run the
///                    deterministic engine on the logged inputs and compare.
/// What it does NOT prove: optimality of the clearing, or the LPs' floorAPR. Those are
/// re-execution properties (V8), not feasibility properties.
contract VeltoraSettlementVerifier {
    VeltoraGateway public immutable gateway;

    event BinSettled(bytes32 indexed marketId, uint64 indexed bin, uint64 coveredCount,
                     bytes32 coveredHash, bytes32 outputHash, bytes32 engineId,
                     uint32 fills, uint32 outcomes, uint32 deltas, uint32 exits);

    error NotOperator();
    error NotMonotone();
    error Coverage(string why);
    error BadOutcome(bytes32 intentId);
    error BadFill(bytes32 intentId, string why);
    error Unsorted();
    error NotConserved(address asset);
    error BadExit(bytes32 positionId);

    constructor(VeltoraGateway gw) { gateway = gw; }

    function settle(bytes32 marketId, T.Settlement calldata s) external {
        (, address operator,,,,, address[] memory assets) = gateway.market(marketId);
        if (msg.sender != operator) revert NotOperator();                       // V1
        uint64 last = gateway.lastSettledBin(marketId);
        if (last != type(uint64).max && s.bin <= last) revert NotMonotone();     // V2
        _checkCoverage(marketId, s);                                             // V3
        _applyOutcomes(marketId, s);                                             // V4
        int256[] memory userNet = _applyFills(marketId, s, assets);              // V4 + V5
        int256[] memory deltaNet = _applyDeltas(marketId, s, assets);            // V7
        for (uint256 k = 0; k < assets.length; k++) {                            // V6
            if (userNet[k] != deltaNet[k]) revert NotConserved(assets[k]);
        }
        _applyExits(marketId, s);
        gateway.hookSettled(marketId, s.bin, s.coveredCount);
        _emitSettled(marketId, s);                                               // V8
    }

    function _applyOutcomes(bytes32 marketId, T.Settlement calldata s) internal {
        for (uint256 i = 0; i < s.outcomes.length; i++) {
            T.Outcome calldata o = s.outcomes[i];
            VeltoraGateway.IntentRecord memory r = gateway.intent(o.intentId);
            if (r.marketId != marketId || r.status != T.ST_OPEN || r.kind == T.KIND_USER_SWAP
                || r.seq > s.coveredCount) revert BadOutcome(o.intentId);
            if (o.accepted && s.bin > r.expiryBin) revert BadOutcome(o.intentId);
            gateway.hookOutcome(marketId, o.intentId, o.accepted);
        }
    }

    function _applyFills(bytes32 marketId, T.Settlement calldata s, address[] memory assets)
        internal returns (int256[] memory userNet)
    {
        userNet = new int256[](assets.length);
        for (uint256 i = 0; i < s.fills.length; i++) {
            T.Fill calldata f = s.fills[i];
            VeltoraGateway.IntentRecord memory r = gateway.intent(f.intentId);
            if (r.marketId != marketId || r.status != T.ST_OPEN || r.kind != T.KIND_USER_SWAP)
                revert BadFill(f.intentId, "state");
            if (r.seq > s.coveredCount || s.bin > r.expiryBin) revert BadFill(f.intentId, "window");
            if (f.sendUsed == 0 || f.sendUsed > r.amounts[0]) revert BadFill(f.intentId, "escrow");
            // both legs enter the signed per-asset sums below; an amount above int256's range would wrap
            // (recv = 2^256 - k counts as -k) and let a settlement pass V6 while crediting an LP with units that
            // do not exist. Refuse it before any arithmetic.
            if (f.sendUsed > uint256(type(int256).max) || f.recvAmount > uint256(type(int256).max))
                revert BadFill(f.intentId, "range");
            // limit: recv/sendUsed >= pMin, rounded in the user's favour
            if (f.recvAmount < Math.mulDiv(f.sendUsed, r.pMinX18, T.X18, Math.Rounding.Ceil))
                revert BadFill(f.intentId, "limit");
            userNet[_idx(assets, r.assets[0])] += int256(f.sendUsed);
            userNet[_idx(assets, r.recvAsset)] -= int256(f.recvAmount);
            gateway.hookFill(marketId, f.intentId, f.sendUsed, f.recvAmount);
        }
    }

    function _applyDeltas(bytes32 marketId, T.Settlement calldata s, address[] memory assets)
        internal returns (int256[] memory deltaNet)
    {
        deltaNet = new int256[](assets.length);
        for (uint256 i = 0; i < s.deltas.length; i++) {
            T.PositionDelta calldata d = s.deltas[i];
            if (i > 0) {
                T.PositionDelta calldata q = s.deltas[i - 1];
                if (d.positionId < q.positionId || (d.positionId == q.positionId && d.asset <= q.asset))
                    revert Unsorted();
            }
            VeltoraGateway.Position memory p = gateway.position(d.positionId);
            if (!p.open || p.marketId != marketId) revert BadExit(d.positionId);
            deltaNet[_idx(assets, d.asset)] += d.delta;
            gateway.hookDelta(marketId, d.positionId, d.asset, d.delta);          // reverts if < 0
        }
    }

    function _applyExits(bytes32 marketId, T.Settlement calldata s) internal {
        for (uint256 i = 0; i < s.exits.length; i++) {
            VeltoraGateway.Position memory p = gateway.position(s.exits[i]);
            if (!p.open || p.marketId != marketId) revert BadExit(s.exits[i]);
            gateway.hookExit(marketId, s.exits[i]);
        }
    }

    function _emitSettled(bytes32 marketId, T.Settlement calldata s) internal {
        emit BinSettled(marketId, s.bin, s.coveredCount, s.coveredHash, s.outputHash, s.engineId,
                        uint32(s.fills.length), uint32(s.outcomes.length),
                        uint32(s.deltas.length), uint32(s.exits.length));
    }

    function _checkCoverage(bytes32 marketId, T.Settlement calldata s) internal view {
        uint64 prev = gateway.lastCoveredCount(marketId);
        uint64 total = gateway.intentCount(marketId);
        if (s.coveredCount < prev || s.coveredCount > total) revert Coverage("range");
        if (gateway.runningHashAt(marketId, s.coveredCount) != s.coveredHash) revert Coverage("hash");
        // every covered intent was logged no later than this bin ...
        if (s.coveredCount > 0) {
            VeltoraGateway.IntentRecord memory lastIn =
                gateway.intent(gateway.intentAt(marketId, s.coveredCount));
            if (lastIn.bin > s.bin) revert Coverage("look-ahead");
        }
        // ... and every intent logged in or before this bin is covered (bins are
        // monotone in the stream, so checking the first uncovered one suffices).
        if (s.coveredCount < total) {
            VeltoraGateway.IntentRecord memory nextIn =
                gateway.intent(gateway.intentAt(marketId, s.coveredCount + 1));
            if (nextIn.bin <= s.bin) revert Coverage("censored");
        }
    }

    function _idx(address[] memory assets, address a) internal pure returns (uint256) {
        for (uint256 i = 0; i < assets.length; i++) if (assets[i] == a) return i;
        revert NotConserved(a);
    }
}
