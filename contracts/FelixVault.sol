// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20, IERC4626} from "./interfaces/IERC4626.sol";

/// @title FelixVault
/// @notice ERC-4626-style tokenised vault that runs delta-neutral funding-rate carry trades
///         across reliable HIP-3 RWA perp markets on Hyperliquid.
///
/// @dev ARCHITECTURE
///      This contract is the on-chain accounting layer only. It:
///        - custodies pooled USDC,
///        - issues / burns vault shares,
///        - tracks accumulated funding yield reported by the operator,
///        - accrues and lets Felix claim an 8% performance fee,
///        - and emits DeployCapital / UnwindCapital events that the off-chain operator bot
///          listens for in order to open / close the *real* short perp legs (and track the
///          *simulated* spot legs) on Hyperliquid testnet.
///
///      The vault never touches Hyperliquid directly. The event stream is the bridge between
///      on-chain deposits and off-chain execution.
///
///      DECIMALS
///        - `asset` (USDC) uses 6 decimals. All USDC amounts in this contract are 6-dp.
///        - shares are 1:1 with USDC on the first deposit, so shares are effectively 6-dp too.
///        - `sharePrice()` / high-water marks are scaled to 1e18 for precision.
contract FelixVault is IERC4626 {
    // --------------------------------------------------------------------------------------
    //                                       STATE
    // --------------------------------------------------------------------------------------

    IERC20 public assetToken;       // USDC on HyperEVM testnet
    address public feeRecipient;    // Felix treasury address
    address public operator;        // address authorised to execute trades and rebalance

    uint256 public totalDeposited;  // total USDC deposited (net of withdrawals)
    uint256 public totalShares;     // total vault shares issued

    // Funding yield accounting
    uint256 public accumulatedYield; // total funding received (cumulative, USDC 6-dp)
    uint256 public pendingFees;      // 8% fee accrued but not yet claimed (USDC 6-dp)
    uint256 public lastFeeAccrual;   // timestamp of last hourly accrual
    uint256 public feeAccruedYield;  // accumulatedYield watermark already charged a fee
    uint256 public vaultInception;   // timestamp the vault was deployed

    uint256 public constant FEE_BPS = 800;        // 8% in basis points
    uint256 public constant FEE_INTERVAL = 1 hours;
    uint256 public constant BPS_DENOMINATOR = 10_000;
    uint256 public constant PRICE_SCALE = 1e18;    // share-price fixed-point scale
    uint256 public constant MIN_DEPLOY = 1_000;    // dust floor (USDC 6-dp) for events

    // Per-depositor tracking
    mapping(address => uint256) public shares;
    mapping(address => uint256) public depositTimestamp;
    // high water mark per depositor — performance fee only on new yield above previous peak
    mapping(address => uint256) public highWaterMark;

    // Market allocation tracking
    struct MarketAllocation {
        string coin;            // e.g. "AAPL"
        string dex;             // e.g. "xyz"
        uint256 allocatedUSDC;  // USDC allocated to this market
        int256 perpSize;        // perp position size (negative = short), scaled 1e8
        uint256 lastRebalance;  // timestamp
        bool active;
    }
    mapping(bytes32 => MarketAllocation) public allocations;
    bytes32[] public activeMarkets;

    // --------------------------------------------------------------------------------------
    //                                       EVENTS
    // --------------------------------------------------------------------------------------

    event Deposit(address indexed user, uint256 usdc, uint256 shares);
    event Withdrawal(address indexed user, uint256 shares, uint256 usdc, uint256 fee);
    event FeesAccrued(uint256 amount, uint256 timestamp);
    event FeesClaimed(uint256 amount, uint256 timestamp);
    event YieldReported(bytes32 indexed marketId, uint256 amount, uint256 timestamp);
    event DeployCapital(bytes32 indexed marketId, uint256 usdcAmount);
    event UnwindCapital(bytes32 indexed marketId, uint256 proportion);
    event MarketAdded(bytes32 indexed marketId, string coin, string dex);
    event MarketRemoved(bytes32 indexed marketId, string coin, string dex);
    event PerpSizeUpdated(bytes32 indexed marketId, int256 perpSize, uint256 timestamp);

    // --------------------------------------------------------------------------------------
    //                                      MODIFIERS
    // --------------------------------------------------------------------------------------

    modifier onlyOperator() {
        require(msg.sender == operator, "FelixVault: not operator");
        _;
    }

    // --------------------------------------------------------------------------------------
    //                                     CONSTRUCTOR
    // --------------------------------------------------------------------------------------

    constructor(address _asset, address _feeRecipient, address _operator) {
        require(_asset != address(0), "FelixVault: zero asset");
        require(_feeRecipient != address(0), "FelixVault: zero feeRecipient");
        require(_operator != address(0), "FelixVault: zero operator");
        assetToken = IERC20(_asset);
        feeRecipient = _feeRecipient;
        operator = _operator;
        lastFeeAccrual = block.timestamp;
        vaultInception = block.timestamp;
    }

    // --------------------------------------------------------------------------------------
    //                                   ERC-4626 SURFACE
    // --------------------------------------------------------------------------------------

    /// @inheritdoc IERC4626
    function asset() external view override returns (address) {
        return address(assetToken);
    }

    /// @notice Net assets under management, in USDC (6-dp).
    /// @dev totalDeposited + accumulatedYield - pendingFees. pendingFees is owed to Felix and
    ///      therefore excluded from depositor-claimable assets.
    function totalAssets() public view override returns (uint256) {
        uint256 gross = totalDeposited + accumulatedYield;
        // Guard against underflow if fees somehow exceed gross (should never happen).
        if (pendingFees >= gross) return 0;
        return gross - pendingFees;
    }

    // --------------------------------------------------------------------------------------
    //                                       DEPOSIT
    // --------------------------------------------------------------------------------------

    /// @notice Deposit USDC and receive vault shares; the new capital is immediately scheduled
    ///         for deployment across active markets via DeployCapital events.
    function deposit(uint256 usdcAmount) external override returns (uint256 sharesIssued) {
        require(usdcAmount > 0, "FelixVault: zero deposit");

        // 1. Pull USDC from the user. totalDeposited is tracked manually, so totalAssets()
        //    still reflects pre-deposit state at this point (correct for share pricing).
        require(
            assetToken.transferFrom(msg.sender, address(this), usdcAmount),
            "FelixVault: transferFrom failed"
        );

        // 2. Calculate shares to issue against pre-deposit assets.
        uint256 assetsBefore = totalAssets();
        if (totalShares == 0 || assetsBefore == 0) {
            sharesIssued = usdcAmount; // 1:1 on the first deposit
        } else {
            sharesIssued = (usdcAmount * totalShares) / assetsBefore;
        }
        require(sharesIssued > 0, "FelixVault: zero shares");

        // 3-4. Mint shares and book the deposit.
        shares[msg.sender] += sharesIssued;
        totalShares += sharesIssued;
        totalDeposited += usdcAmount;
        depositTimestamp[msg.sender] = block.timestamp;

        // 5. Reset the depositor high-water mark to the current share price. Performance fee
        //    on withdrawal is only charged on gains above this mark.
        highWaterMark[msg.sender] = sharePrice();

        // 6. Announce.
        emit Deposit(msg.sender, usdcAmount, sharesIssued);

        // 7. Schedule deployment of the new capital.
        _deployCapital(usdcAmount);
    }

    // --------------------------------------------------------------------------------------
    //                                      WITHDRAWAL
    // --------------------------------------------------------------------------------------

    /// @notice Burn `sharesAmount` shares and receive the proportional USDC, net of the 8%
    ///         performance fee on profit above the depositor high-water mark.
    function withdraw(uint256 sharesAmount) external override returns (uint256 usdcReturned) {
        // 1. Validate balance.
        require(sharesAmount > 0, "FelixVault: zero shares");
        require(shares[msg.sender] >= sharesAmount, "FelixVault: insufficient shares");
        require(totalShares > 0, "FelixVault: no shares outstanding");

        // 2. USDC owed for the redeemed shares.
        uint256 usdcOwed = (sharesAmount * totalAssets()) / totalShares;

        // 3. Performance fee on profit above the high-water mark (no fee on losses).
        uint256 userCostBasis = (sharesAmount * highWaterMark[msg.sender]) / PRICE_SCALE;
        uint256 fee = 0;
        if (usdcOwed > userCostBasis) {
            uint256 profit = usdcOwed - userCostBasis;
            fee = (profit * FEE_BPS) / BPS_DENOMINATOR;
            pendingFees += fee;
            usdcReturned = usdcOwed - fee;
        } else {
            usdcReturned = usdcOwed;
        }

        // 4-5. Burn shares and reduce booked deposits proportionally. The yield portion of
        //      `usdcOwed` is drawn from accumulatedYield; the principal portion from
        //      totalDeposited, keeping totalAssets() internally consistent.
        uint256 principalPortion = (totalDeposited * sharesAmount) / totalShares;
        shares[msg.sender] -= sharesAmount;
        totalShares -= sharesAmount;
        totalDeposited -= principalPortion;

        // Whatever of usdcOwed exceeds the returned principal is paid out of accrued yield.
        uint256 yieldPortion = usdcOwed > principalPortion ? usdcOwed - principalPortion : 0;
        if (yieldPortion > 0) {
            accumulatedYield = accumulatedYield > yieldPortion
                ? accumulatedYield - yieldPortion
                : 0;
            // Keep the fee watermark from exceeding remaining yield.
            if (feeAccruedYield > accumulatedYield) feeAccruedYield = accumulatedYield;
        }

        // 6. Schedule proportional unwind across all active markets (proportion scaled 1e18).
        uint256 proportion = (sharesAmount * PRICE_SCALE) / (totalShares + sharesAmount);
        _unwindCapital(proportion);

        // 7. Pay the user.
        require(assetToken.transfer(msg.sender, usdcReturned), "FelixVault: transfer failed");

        // 8. Announce.
        emit Withdrawal(msg.sender, sharesAmount, usdcReturned, fee);
    }

    // --------------------------------------------------------------------------------------
    //                                    FEE ACCRUAL
    // --------------------------------------------------------------------------------------

    /// @notice Accrue the 8% performance fee on funding yield earned since the last accrual.
    /// @dev Keeper-style: callable by anyone, but typically called hourly by the operator bot.
    ///      Charges 8% of the *new* funding yield reported since the previous accrual, prorated
    ///      across the number of whole hours elapsed so the schedule is visibly "hourly".
    function accrueHourlyFees() external {
        uint256 elapsed = block.timestamp - lastFeeAccrual;
        require(elapsed >= FEE_INTERVAL, "FelixVault: too soon");

        // New funding yield not yet charged a fee.
        uint256 newYield = accumulatedYield > feeAccruedYield
            ? accumulatedYield - feeAccruedYield
            : 0;

        if (newYield > 0) {
            uint256 fee = (newYield * FEE_BPS) / BPS_DENOMINATOR;
            pendingFees += fee;
            feeAccruedYield = accumulatedYield;
            emit FeesAccrued(fee, block.timestamp);
        } else {
            emit FeesAccrued(0, block.timestamp);
        }

        // Advance the accrual clock by whole hours so it stays aligned to the hour grid.
        uint256 hoursElapsed = elapsed / FEE_INTERVAL;
        lastFeeAccrual += hoursElapsed * FEE_INTERVAL;
    }

    /// @notice Claim accrued fees to the Felix treasury.
    function claimFees() external {
        require(msg.sender == feeRecipient, "FelixVault: not feeRecipient");
        uint256 amount = pendingFees;
        require(amount > 0, "FelixVault: nothing to claim");
        pendingFees = 0;
        require(assetToken.transfer(feeRecipient, amount), "FelixVault: transfer failed");
        emit FeesClaimed(amount, block.timestamp);
    }

    // --------------------------------------------------------------------------------------
    //                               FUNDING YIELD REPORTING
    // --------------------------------------------------------------------------------------

    /// @notice Operator reports funding income collected for a market after a settlement.
    /// @dev The operator is responsible for having transferred the corresponding USDC into the
    ///      vault (in the prototype, funding accrues to the perp account; the bot moves the
    ///      realised amount on-chain). accumulatedYield grows the share price for all holders.
    function reportFundingYield(bytes32 marketId, uint256 yieldAmount) external onlyOperator {
        require(allocations[marketId].active, "FelixVault: market inactive");
        accumulatedYield += yieldAmount;
        allocations[marketId].lastRebalance = block.timestamp;
        emit YieldReported(marketId, yieldAmount, block.timestamp);
    }

    // --------------------------------------------------------------------------------------
    //                              CAPITAL DEPLOYMENT (INTERNAL)
    // --------------------------------------------------------------------------------------

    /// @dev Schedule deployment of `usdcAmount` across active markets. The contract splits the
    ///      amount proportionally by each market's current allocation weight (capacity proxy);
    ///      if no weights exist yet it splits evenly. The operator bot's allocation engine
    ///      performs the precise greedy highest-yield-first sizing off-chain — these events are
    ///      the deployment *signal*, and per-market amounts are advisory.
    function _deployCapital(uint256 usdcAmount) internal {
        uint256 n = activeMarkets.length;
        if (n == 0 || usdcAmount == 0) {
            // No markets yet: capital sits idle until markets are added and the bot redeploys.
            return;
        }

        // Total existing weight (allocatedUSDC) to weight the split; fall back to even split.
        uint256 totalWeight = 0;
        for (uint256 i = 0; i < n; i++) {
            totalWeight += allocations[activeMarkets[i]].allocatedUSDC;
        }

        uint256 distributed = 0;
        for (uint256 i = 0; i < n; i++) {
            bytes32 marketId = activeMarkets[i];
            uint256 amount;
            if (i == n - 1) {
                amount = usdcAmount - distributed; // last market mops up rounding dust
            } else if (totalWeight == 0) {
                amount = usdcAmount / n;
            } else {
                amount = (usdcAmount * allocations[marketId].allocatedUSDC) / totalWeight;
            }
            distributed += amount;
            allocations[marketId].allocatedUSDC += amount;
            if (amount >= MIN_DEPLOY) {
                emit DeployCapital(marketId, amount);
            }
        }
    }

    /// @dev Schedule a proportional unwind across all active markets. `proportion` is scaled to
    ///      1e18 (e.g. 0.5e18 = close 50%). The operator bot closes the matching fraction of the
    ///      real short perp and the simulated spot in each market.
    function _unwindCapital(uint256 proportion) internal {
        uint256 n = activeMarkets.length;
        for (uint256 i = 0; i < n; i++) {
            bytes32 marketId = activeMarkets[i];
            MarketAllocation storage a = allocations[marketId];
            uint256 reduce = (a.allocatedUSDC * proportion) / PRICE_SCALE;
            a.allocatedUSDC = a.allocatedUSDC > reduce ? a.allocatedUSDC - reduce : 0;
            emit UnwindCapital(marketId, proportion);
        }
    }

    // --------------------------------------------------------------------------------------
    //                              MARKET MANAGEMENT (OPERATOR)
    // --------------------------------------------------------------------------------------

    /// @notice Register a reliable market the vault is allowed to deploy into.
    function addMarket(string calldata coin, string calldata dex) external onlyOperator returns (bytes32 marketId) {
        marketId = keccak256(abi.encodePacked(dex, ":", coin));
        require(!allocations[marketId].active, "FelixVault: already active");
        allocations[marketId] = MarketAllocation({
            coin: coin,
            dex: dex,
            allocatedUSDC: 0,
            perpSize: 0,
            lastRebalance: block.timestamp,
            active: true
        });
        activeMarkets.push(marketId);
        emit MarketAdded(marketId, coin, dex);
    }

    /// @notice Deregister a market (e.g. funding fell below the viable threshold).
    function removeMarket(bytes32 marketId) external onlyOperator {
        MarketAllocation storage a = allocations[marketId];
        require(a.active, "FelixVault: not active");
        a.active = false;
        // Swap-remove from the active list.
        uint256 n = activeMarkets.length;
        for (uint256 i = 0; i < n; i++) {
            if (activeMarkets[i] == marketId) {
                activeMarkets[i] = activeMarkets[n - 1];
                activeMarkets.pop();
                break;
            }
        }
        emit MarketRemoved(marketId, a.coin, a.dex);
    }

    /// @notice Operator records the real perp size after opening/closing/rebalancing a leg.
    function reportPerpSize(bytes32 marketId, int256 perpSize) external onlyOperator {
        require(allocations[marketId].active, "FelixVault: market inactive");
        allocations[marketId].perpSize = perpSize;
        allocations[marketId].lastRebalance = block.timestamp;
        emit PerpSizeUpdated(marketId, perpSize, block.timestamp);
    }

    /// @notice Update privileged addresses (operator only; e.g. key rotation).
    function setOperator(address newOperator) external onlyOperator {
        require(newOperator != address(0), "FelixVault: zero operator");
        operator = newOperator;
    }

    // --------------------------------------------------------------------------------------
    //                                    VIEW FUNCTIONS
    // --------------------------------------------------------------------------------------

    /// @notice Current share price scaled to 1e18 (USDC per share).
    function sharePrice() public view returns (uint256) {
        if (totalShares == 0) return PRICE_SCALE; // 1.0 before any deposit
        return (totalAssets() * PRICE_SCALE) / totalShares;
    }

    /// @notice ERC-4626-style alias kept for tooling that expects camelCase variants.
    function currentSharePrice() external view returns (uint256) {
        return sharePrice();
    }

    /// @notice A depositor's current position.
    /// @return usdc   USDC value of the user's shares at the current share price.
    /// @return userShares The user's share balance.
    /// @return yield  Profit above the user's high-water-mark cost basis (0 if underwater).
    function userBalance(address user)
        public
        view
        returns (uint256 usdc, uint256 userShares, uint256 yield)
    {
        userShares = shares[user];
        if (totalShares == 0 || userShares == 0) {
            return (0, userShares, 0);
        }
        usdc = (userShares * totalAssets()) / totalShares;
        uint256 costBasis = (userShares * highWaterMark[user]) / PRICE_SCALE;
        yield = usdc > costBasis ? usdc - costBasis : 0;
    }

    /// @notice All active market allocations.
    function getActiveMarkets() public view returns (MarketAllocation[] memory) {
        uint256 n = activeMarkets.length;
        MarketAllocation[] memory out = new MarketAllocation[](n);
        for (uint256 i = 0; i < n; i++) {
            out[i] = allocations[activeMarkets[i]];
        }
        return out;
    }

    /// @notice Number of active markets.
    function activeMarketCount() external view returns (uint256) {
        return activeMarkets.length;
    }

    /// @notice Deterministic market id from dex + coin (matches addMarket).
    function marketId(string calldata coin, string calldata dex) external pure returns (bytes32) {
        return keccak256(abi.encodePacked(dex, ":", coin));
    }
}
