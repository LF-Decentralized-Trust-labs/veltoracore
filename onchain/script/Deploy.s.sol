// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {VeltoraGateway} from "../src/VeltoraGateway.sol";
import {VeltoraSettlementVerifier} from "../src/VeltoraSettlementVerifier.sol";
import {MockStablecoin} from "../src/MockStablecoin.sol";

/// Testnet deployment of a two-asset VeltoraCore market: USDC (USD leg y) and one
/// risky/foreign asset x (default: a test EURC), 60 s bins. Price is endogenous (no oracle).
/// env: PRIVATE_KEY, OPERATOR; optional USDC_ADDR / X_ADDR (existing test tokens).
contract Deploy is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address operator = vm.envAddress("OPERATOR");
        vm.startBroadcast(pk);
        address usdc = vm.envOr("USDC_ADDR", address(0));
        address x = vm.envOr("X_ADDR", address(0));
        if (usdc == address(0)) usdc = address(new MockStablecoin("Test USD Coin", "tUSDC", 6));
        if (x == address(0)) x = address(new MockStablecoin("Test Euro Coin", "tEURC", 6));
        VeltoraGateway gw = new VeltoraGateway();
        VeltoraSettlementVerifier ver = new VeltoraSettlementVerifier(gw);
        address[] memory assets = new address[](2);
        assets[0] = usdc; assets[1] = x;
        bytes32 mkt = keccak256("veltoracore.sepolia.v1");
        gw.createMarket(mkt, operator, address(ver), uint64(block.timestamp), 60, assets);
        vm.stopBroadcast();
        console2.log("gateway ", address(gw));
        console2.log("verifier", address(ver));
        console2.log("USDC", usdc); console2.log("X", x);
        console2.logBytes32(mkt);
    }
}
