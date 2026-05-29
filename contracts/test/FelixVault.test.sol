// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {FelixVault} from "../FelixVault.sol";
import {IERC20} from "../interfaces/IERC4626.sol";

/// @notice Minimal 6-decimal mock USDC for tests.
contract MockUSDC is IERC20 {
    string public name = "Mock USDC";
    string public symbol = "USDC";
    uint8 public decimals = 6;
    uint256 public override totalSupply;
    mapping(address => uint256) public override balanceOf;
    mapping(address => mapping(address => uint256)) public override allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
        emit Transfer(address(0), to, amount);
    }

    function transfer(address to, uint256 amount) external override returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external override returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external override returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }
}

/// @title FelixVaultTest
/// @notice Foundry test suite covering deposits, shares, yield, fees and withdrawals.
/// @dev Run with: forge test --match-contract FelixVaultTest -vv
contract FelixVaultTest is Test {
    // Locally redeclared so expectEmit works on every 0.8.x (matching by signature/topics).
    event DeployCapital(bytes32 indexed marketId, uint256 usdcAmount);

    MockUSDC usdc;
    FelixVault vault;

    address felix = address(0xFE11);      // fee recipient / treasury
    address operator = address(0x09E7A);  // operator bot
    address alice = address(0xA11CE);
    address bob = address(0xB0B);

    uint256 constant USDC_UNIT = 1e6;
    bytes32 aaplId;

    function setUp() public {
        usdc = new MockUSDC();
        vault = new FelixVault(address(usdc), felix, operator);

        // Register one market so deposits emit DeployCapital.
        vm.prank(operator);
        aaplId = vault.addMarket("AAPL", "xyz");

        // Fund users.
        usdc.mint(alice, 100_000 * USDC_UNIT);
        usdc.mint(bob, 100_000 * USDC_UNIT);
        // Fund the vault with extra USDC to represent funding income that lands in the vault.
        usdc.mint(address(vault), 1_000_000 * USDC_UNIT);
    }

    function _deposit(address who, uint256 amount) internal returns (uint256) {
        vm.startPrank(who);
        usdc.approve(address(vault), amount);
        uint256 s = vault.deposit(amount);
        vm.stopPrank();
        return s;
    }

    function testFirstDepositMintsOneToOne() public {
        uint256 amount = 10_000 * USDC_UNIT;
        uint256 s = _deposit(alice, amount);
        assertEq(s, amount, "first deposit should mint 1:1");
        assertEq(vault.totalShares(), amount);
        assertEq(vault.totalDeposited(), amount);
        assertEq(vault.sharePrice(), 1e18, "share price starts at 1.0");
    }

    function testSharePriceRisesWithYield() public {
        uint256 amount = 10_000 * USDC_UNIT;
        _deposit(alice, amount);

        // Operator reports $1,000 of funding yield.
        vm.prank(operator);
        vault.reportFundingYield(aaplId, 1_000 * USDC_UNIT);

        // totalAssets = 10,000 + 1,000 = 11,000 across 10,000 shares -> 1.1
        assertEq(vault.totalAssets(), 11_000 * USDC_UNIT);
        assertEq(vault.sharePrice(), 1.1e18, "share price should be 1.1");
    }

    function testSecondDepositorGetsFewerShares() public {
        _deposit(alice, 10_000 * USDC_UNIT);
        vm.prank(operator);
        vault.reportFundingYield(aaplId, 1_000 * USDC_UNIT); // price now 1.1

        uint256 bobShares = _deposit(bob, 11_000 * USDC_UNIT);
        // Bob deposits 11,000 at price 1.1 -> 10,000 shares.
        assertEq(bobShares, 10_000 * USDC_UNIT, "shares priced at current NAV");
    }

    function testWithdrawChargesPerformanceFeeOnProfit() public {
        uint256 amount = 10_000 * USDC_UNIT;
        uint256 s = _deposit(alice, amount); // hwm = 1.0

        vm.prank(operator);
        vault.reportFundingYield(aaplId, 1_000 * USDC_UNIT); // price 1.1

        uint256 balBefore = usdc.balanceOf(alice);
        vm.prank(alice);
        uint256 out = vault.withdraw(s);

        // Owed = 11,000. Cost basis = 10,000. Profit = 1,000. Fee = 8% = 80.
        // Returned = 11,000 - 80 = 10,920.
        assertEq(out, 10_920 * USDC_UNIT, "fee deducted from profit");
        assertEq(vault.pendingFees(), 80 * USDC_UNIT, "8% fee pending");
        assertEq(usdc.balanceOf(alice) - balBefore, 10_920 * USDC_UNIT);
    }

    function testNoFeeWhenNoProfit() public {
        uint256 amount = 10_000 * USDC_UNIT;
        uint256 s = _deposit(alice, amount);
        vm.prank(alice);
        uint256 out = vault.withdraw(s);
        assertEq(out, amount, "no profit -> no fee");
        assertEq(vault.pendingFees(), 0);
    }

    function testHourlyFeeAccrual() public {
        _deposit(alice, 10_000 * USDC_UNIT);
        vm.prank(operator);
        vault.reportFundingYield(aaplId, 1_000 * USDC_UNIT);

        vm.warp(block.timestamp + 1 hours);
        vault.accrueHourlyFees();
        // 8% of 1,000 = 80.
        assertEq(vault.pendingFees(), 80 * USDC_UNIT, "hourly accrual charges 8% of new yield");

        // No new yield -> no extra fee on the next accrual.
        vm.warp(block.timestamp + 1 hours);
        vault.accrueHourlyFees();
        assertEq(vault.pendingFees(), 80 * USDC_UNIT, "no double charge");
    }

    function testAccrueTooSoonReverts() public {
        vm.expectRevert(bytes("FelixVault: too soon"));
        vault.accrueHourlyFees();
    }

    function testClaimFeesOnlyByRecipient() public {
        _deposit(alice, 10_000 * USDC_UNIT);
        vm.prank(operator);
        vault.reportFundingYield(aaplId, 1_000 * USDC_UNIT);
        vm.warp(block.timestamp + 1 hours);
        vault.accrueHourlyFees();

        vm.expectRevert(bytes("FelixVault: not feeRecipient"));
        vm.prank(alice);
        vault.claimFees();

        uint256 felixBefore = usdc.balanceOf(felix);
        vm.prank(felix);
        vault.claimFees();
        assertEq(usdc.balanceOf(felix) - felixBefore, 80 * USDC_UNIT);
        assertEq(vault.pendingFees(), 0);
    }

    function testReportYieldOnlyOperator() public {
        vm.expectRevert(bytes("FelixVault: not operator"));
        vm.prank(alice);
        vault.reportFundingYield(aaplId, 1_000 * USDC_UNIT);
    }

    function testDepositEmitsDeployCapital() public {
        uint256 amount = 10_000 * USDC_UNIT;
        vm.startPrank(alice);
        usdc.approve(address(vault), amount);
        vm.expectEmit(true, false, false, true);
        emit DeployCapital(aaplId, amount);
        vault.deposit(amount);
        vm.stopPrank();
    }

    function testPartialWithdrawAndUserBalance() public {
        uint256 s = _deposit(alice, 10_000 * USDC_UNIT);
        vm.prank(operator);
        vault.reportFundingYield(aaplId, 1_000 * USDC_UNIT); // price 1.1

        vm.prank(alice);
        vault.withdraw(s / 2); // withdraw half

        (uint256 usdcVal, uint256 sh, uint256 yield) = vault.userBalance(alice);
        assertEq(sh, s / 2, "half shares remain");
        // Remaining 5,000 shares at price ~1.1 -> ~5,500 value.
        assertApproxEqAbs(usdcVal, 5_500 * USDC_UNIT, 10 * USDC_UNIT);
        assertGt(yield, 0, "still showing unrealised yield over hwm");
    }
}
