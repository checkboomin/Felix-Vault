"""Deploy FelixVault to HyperEVM testnet.

Usage:
    python scripts/deploy.py

Requires (env / .env):
    HYPEREVM_RPC            default https://rpc.hyperliquid-testnet.xyz/evm
    CHAIN_ID                default 998
    OPERATOR_PRIVATE_KEY    deployer + operator key (testnet only!)
    USDC_TESTNET_ADDRESS    USDC token on HyperEVM testnet
    FELIX_TREASURY_TESTNET  fee recipient
    OPERATOR_ADDRESS        operator (defaults to deployer address)

Compilation: prefers a Hardhat artifact at
    artifacts/contracts/FelixVault.sol/FelixVault.json
If absent, falls back to py-solc-x to compile contracts/FelixVault.sol on the fly.

Writes the deployed address + ABI to deployment.json for the bot / dashboard.
"""

from __future__ import annotations

import json
import os
import sys

# Add the repo root so the `bot` package (and its shared config) is importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import CONFIG  # noqa: E402

ARTIFACT_PATH = "artifacts/contracts/FelixVault.sol/FelixVault.json"


def load_or_compile():
    """Return (abi, bytecode). Prefer Hardhat artifact, else compile with solcx."""
    if os.path.exists(ARTIFACT_PATH):
        with open(ARTIFACT_PATH) as fh:
            art = json.load(fh)
        bytecode = art["bytecode"]
        if isinstance(bytecode, dict):
            bytecode = bytecode.get("object", "")
        return art["abi"], bytecode

    print("   Hardhat artifact not found — compiling with py-solc-x ...")
    from solcx import compile_standard, install_solc

    install_solc("0.8.20")
    sources = {}
    base = os.path.join(os.path.dirname(__file__), "..", "contracts")
    for rel in [
        "FelixVault.sol",
        "interfaces/IERC4626.sol",
        "interfaces/IHyperliquidPerp.sol",
    ]:
        with open(os.path.join(base, rel)) as fh:
            sources[f"contracts/{rel}"] = {"content": fh.read()}

    compiled = compile_standard(
        {
            "language": "Solidity",
            "sources": sources,
            "settings": {
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
                "optimizer": {"enabled": True, "runs": 200},
                "remappings": [],
            },
        },
        solc_version="0.8.20",
        allow_paths=base,
    )
    c = compiled["contracts"]["contracts/FelixVault.sol"]["FelixVault"]
    return c["abi"], c["evm"]["bytecode"]["object"]


def deploy_vault(asset: str, fee_recipient: str, operator: str):
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(CONFIG.HYPEREVM_RPC))
    if not w3.is_connected():
        raise SystemExit(f"Cannot connect to RPC {CONFIG.HYPEREVM_RPC}")

    acct = w3.eth.account.from_key(CONFIG.OPERATOR_PRIVATE_KEY)
    abi, bytecode = load_or_compile()

    Vault = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = Vault.constructor(
        Web3.to_checksum_address(asset),
        Web3.to_checksum_address(fee_recipient),
        Web3.to_checksum_address(operator),
    ).build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 4_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": CONFIG.CHAIN_ID,
        }
    )
    signed = acct.sign_transaction(tx)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"   Deploy tx: {txh.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    address = receipt.contractAddress

    with open("deployment.json", "w") as fh:
        json.dump(
            {
                "address": address,
                "abi": abi,
                "chainId": CONFIG.CHAIN_ID,
                "rpc": CONFIG.HYPEREVM_RPC,
                "asset": asset,
                "feeRecipient": fee_recipient,
                "operator": operator,
            },
            fh,
            indent=2,
        )

    class Deployed:
        def __init__(self, addr, contract):
            self.address = addr
            self.contract = contract

    return Deployed(address, w3.eth.contract(address=address, abi=abi))


def main():
    asset = os.environ.get("USDC_TESTNET_ADDRESS", "")
    fee_recipient = os.environ.get("FELIX_TREASURY_TESTNET", "")
    operator = os.environ.get("OPERATOR_ADDRESS", "")

    if not CONFIG.OPERATOR_PRIVATE_KEY:
        raise SystemExit("OPERATOR_PRIVATE_KEY not set")
    if not operator:
        from web3 import Web3

        operator = Web3().eth.account.from_key(CONFIG.OPERATOR_PRIVATE_KEY).address
    if not asset or not fee_recipient:
        raise SystemExit("Set USDC_TESTNET_ADDRESS and FELIX_TREASURY_TESTNET")

    print("Deploying FelixVault to HyperEVM testnet...")
    print(f"   asset(USDC):  {asset}")
    print(f"   feeRecipient: {fee_recipient}")
    print(f"   operator:     {operator}")
    vault = deploy_vault(asset, fee_recipient, operator)
    print(f"   Vault deployed at: {vault.address}")
    print("   Wrote deployment.json (set VAULT_ADDRESS to this for the bot/dashboard)")


if __name__ == "__main__":
    main()
