// Hardhat deploy script (alternative to scripts/deploy.py).
//   npx hardhat compile
//   npx hardhat run --network hyperEvmTestnet scripts/deploy.hardhat.js
//
// Reads deploy inputs from env (see .env.example) and writes deployment.json
// (address + ABI) for the Python bot and the dashboard.

const fs = require("fs");
const hre = require("hardhat");

async function main() {
  const asset = process.env.USDC_TESTNET_ADDRESS;
  const feeRecipient = process.env.FELIX_TREASURY_TESTNET;
  let operator = process.env.OPERATOR_ADDRESS;

  if (!asset || !feeRecipient) {
    throw new Error("Set USDC_TESTNET_ADDRESS and FELIX_TREASURY_TESTNET in .env");
  }

  const [deployer] = await hre.ethers.getSigners();
  if (!operator) operator = deployer.address;

  console.log("Deploying FelixVault to HyperEVM testnet...");
  console.log("   deployer:    ", deployer.address);
  console.log("   asset(USDC): ", asset);
  console.log("   feeRecipient:", feeRecipient);
  console.log("   operator:    ", operator);

  const Vault = await hre.ethers.getContractFactory("FelixVault");
  const vault = await Vault.deploy(asset, feeRecipient, operator);
  await vault.waitForDeployment();
  const address = await vault.getAddress();
  console.log("   Vault deployed at:", address);

  const artifact = await hre.artifacts.readArtifact("FelixVault");
  fs.writeFileSync(
    "deployment.json",
    JSON.stringify(
      {
        address,
        abi: artifact.abi,
        chainId: hre.network.config.chainId,
        rpc: hre.network.config.url,
        asset,
        feeRecipient,
        operator,
      },
      null,
      2
    )
  );
  console.log("   Wrote deployment.json");
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
