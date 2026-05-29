require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

/**
 * Hardhat config for the Felix vault prototype.
 *
 * Compiles contracts/FelixVault.sol and deploys to HyperEVM testnet (chain 998).
 * The Python bot/dashboard read the ABI from the generated artifact at
 * artifacts/contracts/FelixVault.sol/FelixVault.json.
 *
 * Foundry can also be used for the Solidity tests in contracts/test (forge test).
 */

const OPERATOR_PRIVATE_KEY = process.env.OPERATOR_PRIVATE_KEY || "";

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.20",
    settings: {
      optimizer: { enabled: true, runs: 200 },
    },
  },
  paths: {
    sources: "./contracts",
    tests: "./test", // JS/TS tests; Solidity tests live in contracts/test (run via forge)
    cache: "./cache",
    artifacts: "./artifacts",
  },
  networks: {
    hyperEvmTestnet: {
      url: process.env.HYPEREVM_RPC || "https://rpc.hyperliquid-testnet.xyz/evm",
      chainId: 998,
      accounts: OPERATOR_PRIVATE_KEY ? [OPERATOR_PRIVATE_KEY] : [],
    },
  },
};
