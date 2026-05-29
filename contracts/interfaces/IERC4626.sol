// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Minimal ERC-20 interface used by the vault for the underlying asset.
interface IERC20 {
    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
}

/// @notice The subset of the ERC-4626 tokenised vault standard implemented by FelixVault.
/// @dev FelixVault diverges from full ERC-4626 (it does not itself implement ERC-20 share
///      transfers) but keeps the core accounting surface so tooling can reason about it.
interface IERC4626 {
    /// @return assetTokenAddress The address of the underlying asset (USDC).
    function asset() external view returns (address assetTokenAddress);

    /// @return totalManagedAssets The total amount of underlying assets managed by the vault.
    function totalAssets() external view returns (uint256 totalManagedAssets);

    /// @notice Deposit `assets` of the underlying token and receive vault shares.
    function deposit(uint256 assets) external returns (uint256 shares);

    /// @notice Burn `shares` and receive the proportional amount of underlying assets.
    function withdraw(uint256 shares) external returns (uint256 assets);
}
