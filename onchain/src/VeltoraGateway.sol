// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {VeltoraTypes as T} from "./VeltoraTypes.sol";

/// @title VeltoraGateway — intake, escrow and custody for VeltoraCore markets.
/// @notice Emits the ONE standard log (`IntentSubmitted`) that every relayer reads.
///         Holds all tokens. State changes from settlement are accepted ONLY from the
///         market's verifier contract, which is where the checks live (so the verifier
///         can be replaced — e.g. by a proof-carrying verifier — without migrating funds).
contract VeltoraGateway is ReentrancyGuard {
    using SafeERC20 for IERC20;

    struct Market {
        address admin;
        address operator;      // the engine key allowed to submit settlements (via verifier)
        address verifier;      // contract that checks + applies settlements
        uint64 genesis;        // unix time of bin 0
        uint32 binSeconds;     // 60 for the testnet
        bool active;
        address[] assets;
    }

    struct IntentRecord {
        bytes32 marketId;
        address owner;
        uint8 kind;
        uint8 status;
        uint64 bin;            // bin of the block that logged it
        uint64 expiryBin;
        uint64 seq;            // 1-based position in the market's intent stream
        address recvAsset;     // USER_SWAP only
        uint256 pMinX18;       // USER_SWAP only
        bytes32 positionId;    // WITHDRAW target
        address[] assets;
        uint256[] amounts;
    }

    struct Position {
        bytes32 marketId;
        address owner;
        bool open;
    }

    // ---- storage ------------------------------------------------------------
    mapping(bytes32 => Market) internal _markets;
    mapping(bytes32 => mapping(address => bool)) public isMarketAsset;
    mapping(bytes32 => IntentRecord) internal _intents;
    mapping(bytes32 => mapping(uint64 => bytes32)) public intentAt;       // market => seq => intentId
    mapping(bytes32 => mapping(uint64 => bytes32)) public runningHashAt;  // market => seq => hash
    mapping(bytes32 => uint64) public intentCount;
    mapping(bytes32 => uint64) public lastSettledBin;                     // type(uint64).max = none yet
    mapping(bytes32 => uint64) public lastCoveredCount;
    mapping(address => mapping(bytes32 => uint64)) public nonces;         // owner => market => last nonce
    mapping(bytes32 => Position) internal _positions;
    mapping(bytes32 => mapping(address => uint256)) public positionBalance; // position => asset => units
    mapping(bytes32 => mapping(address => uint256)) public poolBalance;     // market => asset => units
    mapping(address => mapping(address => uint256)) public claimable;      // owner => asset => units

    // ---- the standard log ---------------------------------------------------
    /// @notice THE standard log. `intent` is the exact struct the owner submitted
    ///         (and, for signed relay, the exact struct whose EIP-712 digest was signed).
    event IntentSubmitted(
        bytes32 indexed marketId,
        bytes32 indexed intentId,
        address indexed owner,
        uint64 seq,
        uint64 bin,
        T.Intent intent
    );
    event MarketCreated(bytes32 indexed marketId, address operator, address verifier,
                        uint64 genesis, uint32 binSeconds, address[] assets);
    event Reclaimed(bytes32 indexed intentId, address indexed owner);
    event Claimed(address indexed owner, address indexed asset, uint256 amount);

    error NotAdmin();
    error NotVerifier();
    error BadMarket();
    error BadIntent(string why);

    modifier onlyVerifier(bytes32 marketId) {
        if (msg.sender != _markets[marketId].verifier) revert NotVerifier();
        _;
    }

    // ---- markets ------------------------------------------------------------
    function createMarket(bytes32 marketId, address operator, address verifier,
                          uint64 genesis, uint32 binSeconds, address[] calldata assets)
        external
    {
        Market storage m = _markets[marketId];
        if (m.admin != address(0)) revert BadMarket();
        if (binSeconds == 0 || assets.length < 2 || verifier == address(0)) revert BadMarket();
        m.admin = msg.sender;
        m.operator = operator;
        m.verifier = verifier;
        m.genesis = genesis;
        m.binSeconds = binSeconds;
        m.active = true;
        for (uint256 i = 0; i < assets.length; i++) {
            if (isMarketAsset[marketId][assets[i]]) revert BadMarket();
            isMarketAsset[marketId][assets[i]] = true;
            m.assets.push(assets[i]);
        }
        lastSettledBin[marketId] = type(uint64).max;
        emit MarketCreated(marketId, operator, verifier, genesis, binSeconds, assets);
    }

    function setActive(bytes32 marketId, bool active) external {
        if (msg.sender != _markets[marketId].admin) revert NotAdmin();
        _markets[marketId].active = active;
    }

    function currentBin(bytes32 marketId) public view returns (uint64) {
        Market storage m = _markets[marketId];
        if (block.timestamp < m.genesis) return 0;
        return uint64((block.timestamp - m.genesis) / m.binSeconds);
    }

    // ---- intake -------------------------------------------------------------
    /// @notice Submit an intent as msg.sender. Escrow is pulled here; the engine
    ///         never touches funds except through a verified settlement.
    function submit(T.Intent calldata it) external nonReentrant returns (bytes32 intentId) {
        Market storage m = _markets[it.marketId];
        if (!m.active) revert BadMarket();
        if (it.assets.length != it.amounts.length) revert BadIntent("legs");
        if (it.nonce <= nonces[msg.sender][it.marketId]) revert BadIntent("nonce");
        uint64 bin = currentBin(it.marketId);
        if (it.expiryBin < bin) revert BadIntent("expired");
        nonces[msg.sender][it.marketId] = it.nonce;

        intentId = keccak256(abi.encode(block.chainid, address(this), msg.sender, T.structHash(it)));
        if (_intents[intentId].status != T.ST_NONE) revert BadIntent("dup");

        IntentRecord storage r = _intents[intentId];
        r.marketId = it.marketId;
        r.owner = msg.sender;
        r.kind = it.kind;
        r.status = T.ST_OPEN;
        r.bin = bin;
        r.expiryBin = it.expiryBin;

        if (it.kind == T.KIND_USER_SWAP) {
            if (it.assets.length != 1 || it.amounts[0] == 0) revert BadIntent("swap legs");
            (address recv, uint256 pMin,,) = abi.decode(it.terms, (address, uint256, bytes32, bytes32));
            if (!isMarketAsset[it.marketId][recv] || recv == it.assets[0] || pMin == 0)
                revert BadIntent("swap terms");
            r.recvAsset = recv;
            r.pMinX18 = pMin;
        } else if (it.kind == T.KIND_LP_COMMIT) {
            if (it.assets.length == 0) revert BadIntent("no capital");
        } else if (it.kind == T.KIND_LP_WITHDRAW) {
            bytes32 pid = abi.decode(it.terms, (bytes32));
            Position storage p = _positions[pid];
            if (!p.open || p.owner != msg.sender || p.marketId != it.marketId) revert BadIntent("position");
            if (it.assets.length != 0) revert BadIntent("withdraw escrow");
            r.positionId = pid;
        } else {
            revert BadIntent("kind");
        }

        for (uint256 i = 0; i < it.assets.length; i++) {
            address a = it.assets[i];
            if (!isMarketAsset[it.marketId][a]) revert BadIntent("asset");
            for (uint256 j = 0; j < i; j++) if (it.assets[j] == a) revert BadIntent("dup asset");
            if (it.amounts[i] > 0) IERC20(a).safeTransferFrom(msg.sender, address(this), it.amounts[i]);
            r.assets.push(a);
            r.amounts.push(it.amounts[i]);
        }

        uint64 seq = ++intentCount[it.marketId];
        r.seq = seq;
        intentAt[it.marketId][seq] = intentId;
        runningHashAt[it.marketId][seq] =
            keccak256(abi.encodePacked(runningHashAt[it.marketId][seq - 1], intentId));

        _emitSubmitted(it, intentId, seq, bin);
    }

    function _emitSubmitted(T.Intent calldata it, bytes32 intentId, uint64 seq, uint64 bin) private {
        emit IntentSubmitted(it.marketId, intentId, msg.sender, seq, bin, it);
    }

    /// @notice Reclaim escrow of an OPEN intent once every bin it could execute in
    ///         has been settled. No race with the engine is possible.
    function reclaim(bytes32 intentId) external nonReentrant {
        IntentRecord storage r = _intents[intentId];
        if (r.owner != msg.sender || r.status != T.ST_OPEN) revert BadIntent("reclaim");
        uint64 last = lastSettledBin[r.marketId];
        if (last == type(uint64).max || last < r.expiryBin) revert BadIntent("not yet");
        r.status = T.ST_RECLAIMED;
        for (uint256 i = 0; i < r.assets.length; i++) claimable[msg.sender][r.assets[i]] += r.amounts[i];
        emit Reclaimed(intentId, msg.sender);
    }

    function claim(address asset) external nonReentrant {
        uint256 amt = claimable[msg.sender][asset];
        claimable[msg.sender][asset] = 0;
        if (amt > 0) IERC20(asset).safeTransfer(msg.sender, amt);
        emit Claimed(msg.sender, asset, amt);
    }

    // ---- views (used by the verifier and the relayer) -----------------------
    function market(bytes32 marketId) external view returns (
        address admin, address operator, address verifier, uint64 genesis, uint32 binSeconds,
        bool active, address[] memory assets)
    {
        Market storage m = _markets[marketId];
        return (m.admin, m.operator, m.verifier, m.genesis, m.binSeconds, m.active, m.assets);
    }

    function intent(bytes32 intentId) external view returns (IntentRecord memory) {
        return _intents[intentId];
    }

    function position(bytes32 positionId) external view returns (Position memory) {
        return _positions[positionId];
    }

    // ---- settlement hooks (verifier only) -----------------------------------
    function hookOutcome(bytes32 marketId, bytes32 intentId, bool accepted)
        external onlyVerifier(marketId)
    {
        IntentRecord storage r = _intents[intentId];
        if (!accepted) {
            r.status = T.ST_REJECTED;
            for (uint256 i = 0; i < r.assets.length; i++) claimable[r.owner][r.assets[i]] += r.amounts[i];
            return;
        }
        r.status = T.ST_ACCEPTED;
        bytes32 pid = r.kind == T.KIND_LP_COMMIT ? intentId : r.positionId;
        if (r.kind == T.KIND_LP_COMMIT) _positions[pid] = Position(marketId, r.owner, true);
        for (uint256 i = 0; i < r.assets.length; i++) {
            positionBalance[pid][r.assets[i]] += r.amounts[i];
            poolBalance[marketId][r.assets[i]] += r.amounts[i];
        }
        // LP_WITHDRAW acceptance carries no funds; the exit itself arrives in `exits`.
    }

    function hookFill(bytes32 marketId, bytes32 intentId, uint256 sendUsed, uint256 recvAmount)
        external onlyVerifier(marketId)
    {
        IntentRecord storage r = _intents[intentId];
        r.status = T.ST_FILLED;
        address send = r.assets[0];
        uint256 refund = r.amounts[0] - sendUsed;
        if (refund > 0) claimable[r.owner][send] += refund;
        claimable[r.owner][r.recvAsset] += recvAmount;
        // Pool totals move ONLY through position deltas (hookDelta). The verifier
        // proves per asset that sum(user sendUsed) - sum(user recv) == sum(deltas),
        // so the pool stays equal to the sum of positions with no ordering hazard.
    }

    function hookDelta(bytes32 marketId, bytes32 positionId, address asset, int256 delta)
        external onlyVerifier(marketId)
    {
        if (delta >= 0) {
            positionBalance[positionId][asset] += uint256(delta);
            poolBalance[marketId][asset] += uint256(delta);
        } else {
            positionBalance[positionId][asset] -= uint256(-delta); // reverts if negative
            poolBalance[marketId][asset] -= uint256(-delta);
        }
    }

    function hookExit(bytes32 marketId, bytes32 positionId) external onlyVerifier(marketId) {
        Position storage p = _positions[positionId];
        p.open = false;
        address[] storage assets = _markets[marketId].assets;
        for (uint256 i = 0; i < assets.length; i++) {
            uint256 b = positionBalance[positionId][assets[i]];
            if (b == 0) continue;
            positionBalance[positionId][assets[i]] = 0;
            poolBalance[marketId][assets[i]] -= b;
            claimable[p.owner][assets[i]] += b;
        }
    }

    function hookSettled(bytes32 marketId, uint64 bin, uint64 coveredCount)
        external onlyVerifier(marketId)
    {
        lastSettledBin[marketId] = bin;
        lastCoveredCount[marketId] = coveredCount;
    }
}
