// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title IHyperliquidPerp
/// @notice Conceptual interface describing how the off-chain operator bot interacts with
///         Hyperliquid perp markets on behalf of the vault.
/// @dev There is NO on-chain Hyperliquid perp contract that the vault calls directly.
///      Hyperliquid order placement happens off-chain via the HTTP API (signed by the
///      operator key). This interface documents the shape of those interactions so the
///      contract <-> bot bridge is explicit and auditable. The vault communicates intent
///      to the bot purely through the DeployCapital / UnwindCapital events; the bot then
///      performs the equivalent of the calls described below against the HL testnet API.
interface IHyperliquidPerp {
    /// @notice A short perp position opened by the operator against a HIP-3 market.
    /// @param coin       Exact ticker string as returned by HL `meta` (e.g. "AAPL").
    /// @param dex        HIP-3 dex identifier (e.g. "xyz").
    /// @param isBuy      false = short (the carry-trade leg), true = buy-to-close.
    /// @param size       Position size in units of the underlying.
    /// @param limitPx    Limit price (IOC) used to cross the book.
    /// @return filledSize The size actually filled.
    function order(
        string calldata coin,
        string calldata dex,
        bool isBuy,
        uint256 size,
        uint256 limitPx
    ) external returns (uint256 filledSize);

    /// @notice Funding received (in USDC, signed) for a market since `sinceTs`.
    function fundingReceived(string calldata coin, uint256 sinceTs)
        external
        view
        returns (int256 fundingUsdc);

    /// @notice Unrealised PnL of the open short perp for `coin`.
    function positionPnl(string calldata coin) external view returns (int256 pnlUsdc);

    /// @notice Current Hyperliquid mark price for `coin`, scaled to 1e8.
    function markPrice(string calldata coin) external view returns (uint256 markPx);
}
