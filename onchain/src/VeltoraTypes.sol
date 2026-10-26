// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

/// @title VeltoraTypes — the standard intent envelope of VeltoraCore markets.
/// @notice One envelope, many markets. The chain enforces only what it must
///         (escrow legs, the user's limit price, expiry, coverage, conservation);
///         everything market-specific rides in `params` (canonical JSON, RFC 8785)
///         and is validated off-chain by the engine's own validator.
library VeltoraTypes {
    // ---- intent kinds (stable wire codes; never renumber) ------------------
    uint8 internal constant KIND_LP_COMMIT   = 1; // escrow capital, open a position
    // wire code 2 is reserved (unused)
    uint8 internal constant KIND_LP_WITHDRAW = 3; // request exit of a position (no escrow)
    uint8 internal constant KIND_USER_SWAP   = 4; // escrow send leg, limit on recv/send

    // ---- intent status ------------------------------------------------------
    uint8 internal constant ST_NONE      = 0;
    uint8 internal constant ST_OPEN      = 1;
    uint8 internal constant ST_FILLED    = 2; // swap executed
    uint8 internal constant ST_ACCEPTED  = 3; // LP commit / withdraw accepted
    uint8 internal constant ST_REJECTED  = 4; // engine refused; escrow refunded
    uint8 internal constant ST_RECLAIMED = 5; // owner reclaimed after expiry

    /// @dev Fixed-point scale for limit prices: pMinX18 = recv_units * 1e18 / send_units
    ///      in RAW token units (decimals-aware; the relayer converts to human prices).
    uint256 internal constant X18 = 1e18;

    /// @notice What a submitter signs / sends. `terms` is kind-specific ABI:
    ///   USER_SWAP  : abi.encode(address recvAsset, uint256 pMinX18, bytes32 reserved0, bytes32 reserved1)
    ///   LP_COMMIT  : empty
    ///   LP_WITHDRAW: abi.encode(bytes32 positionId)
    struct Intent {
        bytes32 marketId;
        uint8 kind;
        bytes32 schemaId;     // keccak256("veltoracore.<kind>/<version>")
        uint64 nonce;         // per-owner, strictly increasing
        uint64 expiryBin;     // last bin in which the intent may be executed
        address[] assets;     // escrow legs (LP: capital; USER: [sendAsset])
        uint256[] amounts;    // escrow amounts, raw token units
        bytes terms;
        bytes params;         // canonical JSON (engine-only fields)
    }

    bytes32 internal constant INTENT_TYPEHASH = keccak256(
        "Intent(bytes32 marketId,uint8 kind,bytes32 schemaId,uint64 nonce,uint64 expiryBin,"
        "address[] assets,uint256[] amounts,bytes terms,bytes params)"
    );

    /// @notice EIP-712 struct hash (arrays and bytes hashed per the spec), so the
    ///         same digest serves direct submission today and signed relay later.
    function structHash(Intent calldata it) internal pure returns (bytes32) {
        return keccak256(abi.encode(
            INTENT_TYPEHASH, it.marketId, it.kind, it.schemaId, it.nonce, it.expiryBin,
            keccak256(abi.encodePacked(it.assets)),
            keccak256(abi.encodePacked(it.amounts)),
            keccak256(it.terms),
            keccak256(it.params)
        ));
    }

    // ---- settlement (produced by the engine, checked by the verifier) -------
    struct Outcome {            // LP_COMMIT / LP_WITHDRAW
        bytes32 intentId;
        bool accepted;
    }
    struct Fill {               // USER_SWAP
        bytes32 intentId;
        uint256 sendUsed;       // <= escrowed send amount; remainder refunded
        uint256 recvAmount;     // delivered to the user (net of any fee)
    }
    struct PositionDelta {      // signed change of one LP position in one asset
        bytes32 positionId;
        address asset;
        int256 delta;
    }
    struct Settlement {
        uint64 bin;             // the bin being settled (strictly increasing)
        uint64 coveredCount;    // engine attests it saw intents [1..coveredCount]
        bytes32 coveredHash;    // running hash at coveredCount (censorship check)
        bytes32 outputHash;     // hash of the engine's full output (re-execution anchor)
        bytes32 engineId;       // code identity (e.g. keccak of the solver file / commit)
        Outcome[] outcomes;
        Fill[] fills;
        PositionDelta[] deltas;
        bytes32[] exits;        // positions closed this bin; balances -> owner claimable
    }
}
