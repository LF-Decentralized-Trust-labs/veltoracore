// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {VeltoraTypes as T} from "../src/VeltoraTypes.sol";
import {VeltoraGateway} from "../src/VeltoraGateway.sol";
import {VeltoraSettlementVerifier} from "../src/VeltoraSettlementVerifier.sol";
import {MockStablecoin} from "../src/MockStablecoin.sol";

/// Test map — each block covers one property of the on-chain settlement check:
///   conservation (mass balance)  -> test_conservation_*      (per asset, integer, ZERO tolerance)
///   LP position partition        -> test_partition_*, testFuzz_partition
///   intent coverage, once only   -> test_coverage_*, test_once_*
///   no negative balances         -> test_solvency_*
///   user limit price             -> test_limit_*
///   status / anti-tamper         -> test_operator_*, test_monotone_*
contract VeltoraSettlementTest is Test {
    VeltoraGateway gw;
    VeltoraSettlementVerifier ver;
    MockStablecoin usdc;
    MockStablecoin eurc;
    bytes32 constant MKT = keccak256("veltoracore.sepolia.test");
    bytes32 constant SCH_LP = keccak256("veltoracore.lp/1");
    bytes32 constant SCH_USER = keccak256("veltoracore.user/1");
    address operator = address(0xE1);
    address lp = address(0x1001);
    address lp2 = address(0x1002);
    address alice = address(0xA11CE);
    uint64 genesis;
    uint64 nonceCounter;

    function setUp() public {
        vm.warp(1_800_000_000);
        genesis = uint64(block.timestamp);
        gw = new VeltoraGateway();
        ver = new VeltoraSettlementVerifier(gw);
        usdc = new MockStablecoin("Test USD Coin", "tUSDC", 6);
        eurc = new MockStablecoin("Test Euro Coin", "tEURC", 6);
        address[] memory assets = new address[](2);
        assets[0] = address(usdc); assets[1] = address(eurc);
        gw.createMarket(MKT, operator, address(ver), genesis, 60, assets);
        for (uint160 i = 0; i < 3; i++) {
            address who = i == 0 ? lp : (i == 1 ? lp2 : alice);
            usdc.mint(who, 1e12); eurc.mint(who, 1e12);
            vm.startPrank(who);
            usdc.approve(address(gw), type(uint256).max);
            eurc.approve(address(gw), type(uint256).max);
            vm.stopPrank();
        }
    }

    // ---------------------------------------------------------------- helpers
    function _lpCommit(address who, uint256 u, uint256 e) internal returns (bytes32) {
        address[] memory a = new address[](2); a[0] = address(usdc); a[1] = address(eurc);
        uint256[] memory m = new uint256[](2); m[0] = u; m[1] = e;
        T.Intent memory it = T.Intent(MKT, T.KIND_LP_COMMIT, SCH_LP, ++nonceCounter, 1000, a, m, "",
            bytes('{"floorAPR":0.0}'));
        vm.prank(who);
        return gw.submit(it);
    }

    function _swap(address who, address send, uint256 amt, address recv, uint256 pMinX18, uint64 exp)
        internal returns (bytes32)
    {
        address[] memory a = new address[](1); a[0] = send;
        uint256[] memory m = new uint256[](1); m[0] = amt;
        T.Intent memory it = T.Intent(MKT, T.KIND_USER_SWAP, SCH_USER, ++nonceCounter, exp, a, m,
            abi.encode(recv, pMinX18, bytes32(0), bytes32(0)), "");
        vm.prank(who);
        return gw.submit(it);
    }

    function _empty(uint64 bin) internal view returns (T.Settlement memory s) {
        uint64 n = gw.intentCount(MKT);
        s.bin = bin; s.coveredCount = n; s.coveredHash = gw.runningHashAt(MKT, n);
        s.outputHash = keccak256("out"); s.engineId = keccak256("engine");
        s.outcomes = new T.Outcome[](0); s.fills = new T.Fill[](0);
        s.deltas = new T.PositionDelta[](0); s.exits = new bytes32[](0);
    }

    function _settle(T.Settlement memory s) internal {
        vm.prank(operator);
        ver.settle(MKT, s);
    }

    /// bin 0: LP commits 1,000,000 USDC + 900,000 EURC; accepted.
    function _seedPool() internal returns (bytes32 pid) {
        pid = _lpCommit(lp, 1_000_000e6, 900_000e6);
        T.Settlement memory s = _empty(0);
        s.outcomes = new T.Outcome[](1); s.outcomes[0] = T.Outcome(pid, true);
        _settle(s);
    }

    /// A conserving swap settlement: alice sends `sendUsed` EURC, receives `recv` USDC,
    /// the single LP absorbs the exact opposite legs.
    function _swapSettlement(bytes32 pid, bytes32 sw, uint64 bin, uint256 sendUsed, uint256 recv)
        internal view returns (T.Settlement memory s)
    {
        s = _empty(bin);
        s.fills = new T.Fill[](1); s.fills[0] = T.Fill(sw, sendUsed, recv);
        s.deltas = new T.PositionDelta[](2);
        (address a0, address a1) = address(usdc) < address(eurc)
            ? (address(usdc), address(eurc)) : (address(eurc), address(usdc));
        int256 d0 = a0 == address(usdc) ? -int256(recv) : int256(sendUsed);
        int256 d1 = a1 == address(usdc) ? -int256(recv) : int256(sendUsed);
        s.deltas[0] = T.PositionDelta(pid, a0, d0);
        s.deltas[1] = T.PositionDelta(pid, a1, d1);
    }

    function _partitionHolds(bytes32[] memory pids) internal view {
        address[2] memory as_ = [address(usdc), address(eurc)];
        for (uint256 k = 0; k < 2; k++) {
            uint256 sum;
            for (uint256 i = 0; i < pids.length; i++) sum += gw.positionBalance(pids[i], as_[k]);
            assertEq(sum, gw.poolBalance(MKT, as_[k]), "partition: sum(positions) != pool");
        }
    }

    function _bin(uint64 b) internal { vm.warp(genesis + uint256(b) * 60 + 1); }

    // -------------------------------------------------------------- happy path
    function test_happy_swap_settles_and_claims() public {
        bytes32 pid = _seedPool();
        _bin(1);
        // alice sells 1,000 EURC for >= 1.08 USDC/EURC (pMin in raw units, both 6 dp)
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        _settle(_swapSettlement(pid, sw, 1, 1_000e6, 1_082e6));
        assertEq(gw.claimable(alice, address(usdc)), 1_082e6);
        assertEq(gw.positionBalance(pid, address(eurc)), 901_000e6);
        assertEq(gw.positionBalance(pid, address(usdc)), 998_918e6);
        bytes32[] memory p = new bytes32[](1); p[0] = pid; _partitionHolds(p);
        uint256 before = usdc.balanceOf(alice);
        vm.prank(alice); gw.claim(address(usdc));
        assertEq(usdc.balanceOf(alice) - before, 1_082e6);
    }

    function test_partial_send_refunds_remainder() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        _settle(_swapSettlement(pid, sw, 1, 400e6, 433e6));
        assertEq(gw.claimable(alice, address(eurc)), 600e6, "unused send refunded");
    }

    // ----------------------------------------------------- conservation (mass balance)
    function test_conservation_rejects_value_leak() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _swapSettlement(pid, sw, 1, 1_000e6, 1_082e6);
        // LP credited one unit more than users paid in -> value created from nothing
        for (uint256 i = 0; i < 2; i++) if (s.deltas[i].asset == address(eurc)) s.deltas[i].delta += 1;
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.NotConserved.selector, address(eurc)));
        _settle(s);
    }

    function test_conservation_rejects_fee_skim() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _swapSettlement(pid, sw, 1, 1_000e6, 1_082e6);
        // LP loses more USDC than the user receives -> units vanish (someone skimmed)
        for (uint256 i = 0; i < 2; i++) if (s.deltas[i].asset == address(usdc)) s.deltas[i].delta -= 5;
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.NotConserved.selector, address(usdc)));
        _settle(s);
    }

    function test_conservation_cannot_be_bypassed_by_int256_wrap() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        uint256 k = 1_082e6;
        T.Settlement memory s = _swapSettlement(pid, sw, 1, 1_000e6, k);
        s.fills[0].recvAmount = type(uint256).max - k + 1;                  // == 2^256 - k
        for (uint256 i = 0; i < 2; i++) if (s.deltas[i].asset == address(usdc)) s.deltas[i].delta = int256(k);
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.BadFill.selector, sw, "range"));
        _settle(s);
        // the edge: one unit above int256.max is already out of range
        s.fills[0].recvAmount = uint256(type(int256).max) + 1;
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.BadFill.selector, sw, "range"));
        _settle(s);
    }

    // ------------------------------------------------------------- p_min (limit)
    function test_limit_rejects_fill_below_pmin() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        // 1,079.999999 USDC for 1,000 EURC is below 1.08
        T.Settlement memory s = _swapSettlement(pid, sw, 1, 1_000e6, 1_080e6 - 1);
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.BadFill.selector, sw, "limit"));
        _settle(s);
    }

    function test_limit_boundary_exact_pmin_accepted() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        _settle(_swapSettlement(pid, sw, 1, 1_000e6, 1_080e6));
    }

    function test_limit_rejects_overspend_of_escrow() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _swapSettlement(pid, sw, 1, 1_000e6 + 1, 2_000e6);
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.BadFill.selector, sw, "escrow"));
        _settle(s);
    }

    // ------------------------------------------------ solvency (no negative position)
    function test_solvency_rejects_position_below_zero() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 900_000e6, address(usdc), 0.5e18, 5);
        // pay out more USDC than the only position holds
        T.Settlement memory s = _swapSettlement(pid, sw, 1, 900_000e6, 1_000_001e6);
        vm.expectRevert(); // arithmetic underflow in the gateway: solvency is structural
        _settle(s);
    }

    // ------------------------------------------------ coverage
    function test_coverage_rejects_censorship() public {
        _seedPool();
        _bin(1);
        _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _empty(1);
        s.coveredCount -= 1; s.coveredHash = gw.runningHashAt(MKT, s.coveredCount);
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.Coverage.selector, "censored"));
        _settle(s);
    }

    function test_coverage_rejects_lookahead() public {
        _seedPool();
        _bin(3);
        _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _empty(2); // claims to settle bin 2 but covers a bin-3 intent
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.Coverage.selector, "look-ahead"));
        _settle(s);
    }

    function test_coverage_rejects_wrong_hash() public {
        _seedPool();
        _bin(1);
        _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _empty(1);
        s.coveredHash = keccak256("forged");
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.Coverage.selector, "hash"));
        _settle(s);
    }

    function test_coverage_allows_gap_bins() public {
        _seedPool();
        _bin(7);
        _settle(_empty(7)); // bins 1..6 had no intents: one settlement closes them all
        assertEq(gw.lastSettledBin(MKT), 7);
    }

    function test_once_rejects_double_fill() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        _settle(_swapSettlement(pid, sw, 1, 1_000e6, 1_082e6));
        _bin(2);
        T.Settlement memory s2 = _swapSettlement(pid, sw, 2, 1_000e6, 1_082e6);
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.BadFill.selector, sw, "state"));
        _settle(s2);
    }

    function test_once_rejects_expired_fill() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 1);
        _settle(_empty(1));
        _bin(2);
        T.Settlement memory s2 = _swapSettlement(pid, sw, 2, 1_000e6, 1_082e6);
        vm.expectRevert(abi.encodeWithSelector(VeltoraSettlementVerifier.BadFill.selector, sw, "window"));
        _settle(s2);
    }

    function test_deltas_must_be_sorted_unique() public {
        bytes32 pid = _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _swapSettlement(pid, sw, 1, 1_000e6, 1_082e6);
        (s.deltas[0], s.deltas[1]) = (s.deltas[1], s.deltas[0]);
        vm.expectRevert(VeltoraSettlementVerifier.Unsorted.selector);
        _settle(s);
    }

    // ------------------------------------------------ status / anti-tamper
    function test_operator_only() public {
        _seedPool();
        _bin(1);
        T.Settlement memory s = _empty(1);
        vm.prank(alice);
        vm.expectRevert(VeltoraSettlementVerifier.NotOperator.selector);
        ver.settle(MKT, s);
    }

    function test_monotone_bins() public {
        _seedPool();
        T.Settlement memory s = _empty(0);   // build BEFORE expectRevert (it makes view calls)
        vm.expectRevert(VeltoraSettlementVerifier.NotMonotone.selector);
        _settle(s);
    }

    function test_gateway_hooks_reject_non_verifier() public {
        vm.expectRevert(VeltoraGateway.NotVerifier.selector);
        gw.hookSettled(MKT, 99, 0);
    }

    // ------------------------------------------------ LP lifecycle
    function test_lp_reject_refunds_escrow() public {
        bytes32 pid = _lpCommit(lp2, 10e6, 10e6);
        T.Settlement memory s = _empty(0);
        s.outcomes = new T.Outcome[](1); s.outcomes[0] = T.Outcome(pid, false);
        _settle(s);
        assertEq(gw.claimable(lp2, address(usdc)), 10e6);
        assertEq(gw.poolBalance(MKT, address(usdc)), 0);
    }

    function test_lp_withdraw_and_exit_returns_inventory() public {
        bytes32 pid = _seedPool();
        _bin(1);
        address[] memory a = new address[](0); uint256[] memory m = new uint256[](0);
        T.Intent memory it = T.Intent(MKT, T.KIND_LP_WITHDRAW, SCH_LP, ++nonceCounter, 5, a, m,
            abi.encode(pid), "");
        vm.prank(lp); bytes32 w = gw.submit(it);
        T.Settlement memory s = _empty(1);
        s.outcomes = new T.Outcome[](1); s.outcomes[0] = T.Outcome(w, true);
        s.exits = new bytes32[](1); s.exits[0] = pid;
        _settle(s);
        assertEq(gw.claimable(lp, address(usdc)), 1_000_000e6);
        assertEq(gw.claimable(lp, address(eurc)), 900_000e6);
        assertEq(gw.poolBalance(MKT, address(usdc)), 0);
    }

    function test_reclaim_only_after_expiry_settled() public {
        _seedPool();
        _bin(1);
        bytes32 sw = _swap(alice, address(eurc), 1_000e6, address(usdc), 1.08e18, 2);
        vm.prank(alice); vm.expectRevert(); gw.reclaim(sw);
        _bin(3); _settle(_empty(3));
        vm.prank(alice); gw.reclaim(sw);
        assertEq(gw.claimable(alice, address(eurc)), 1_000e6);
    }

    // ------------------------------------------------ partition (fuzz)
    /// Two LPs share each swap pro-rata; after any conserving settlement the
    /// positions must partition the pool exactly.
    function testFuzz_partition(uint96 sendRaw, uint16 bps) public {
        bytes32 p1 = _lpCommit(lp, 1_000_000e6, 900_000e6);
        bytes32 p2 = _lpCommit(lp2, 500_000e6, 450_000e6);
        T.Settlement memory s0 = _empty(0);
        s0.outcomes = new T.Outcome[](2);
        s0.outcomes[0] = T.Outcome(p1, true); s0.outcomes[1] = T.Outcome(p2, true);
        _settle(s0);
        _bin(1);
        uint256 send = bound(uint256(sendRaw), 1, 100_000e6);
        uint256 num = send * (10_800 + bound(uint256(bps), 0, 200));
        uint256 recv = (num + 9_999) / 10_000; // CEIL: the verifier rejects rounding against the user
        bytes32 sw = _swap(alice, address(eurc), send, address(usdc), 1.08e18, 5);
        T.Settlement memory s = _empty(1);
        s.fills = new T.Fill[](1); s.fills[0] = T.Fill(sw, send, recv);
        s.deltas = _proRata(p1, p2, send, recv);
        _settle(s);
        bytes32[] memory pids = new bytes32[](2); pids[0] = p1; pids[1] = p2;
        _partitionHolds(pids);
        // token-level solvency: the gateway holds every unit it owes
        assertEq(usdc.balanceOf(address(gw)),
                 gw.poolBalance(MKT, address(usdc)) + gw.claimable(alice, address(usdc)));
    }

    /// pro-rata 2:1 (p1 has twice p2's capital), rounding residue to the larger LP
    function _proRata(bytes32 p1, bytes32 p2, uint256 send, uint256 recv)
        internal view returns (T.PositionDelta[] memory d)
    {
        int256[2] memory dU = [-int256(recv - recv / 3), -int256(recv / 3)];
        int256[2] memory dE = [int256(send - send / 3), int256(send / 3)];
        bytes32[2] memory pid = p1 < p2 ? [p1, p2] : [p2, p1];
        uint256[2] memory who = p1 < p2 ? [uint256(0), 1] : [uint256(1), 0];
        bool usdcFirst = address(usdc) < address(eurc);
        d = new T.PositionDelta[](4);
        for (uint256 i = 0; i < 2; i++) {
            d[2 * i]     = T.PositionDelta(pid[i], usdcFirst ? address(usdc) : address(eurc),
                                           usdcFirst ? dU[who[i]] : dE[who[i]]);
            d[2 * i + 1] = T.PositionDelta(pid[i], usdcFirst ? address(eurc) : address(usdc),
                                           usdcFirst ? dE[who[i]] : dU[who[i]]);
        }
    }
}
