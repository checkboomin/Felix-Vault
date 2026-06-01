"""Deploy a mock USDC + FelixVault to HyperEVM testnet, and mint demo USDC.

Usage:
    python scripts/deploy_mock.py

This is the one-command path for a self-contained testnet demo that does NOT
depend on bridging HyperCore USDC to HyperEVM:

  1. deploy MockUSDC (6-decimal ERC-20, open mint)
  2. mint MINT_AMOUNT USDC to the operator/deployer wallet
  3. deploy FelixVault pointing at the MockUSDC
  4. write deployment.json (vault address + ABI) for the bot / dashboard

Env (same as deploy.py):
    HYPEREVM_RPC, CHAIN_ID, OPERATOR_PRIVATE_KEY,
    FELIX_TREASURY_TESTNET, OPERATOR_ADDRESS (optional),
    MINT_AMOUNT (optional, default 100000 USDC)

Requires Hardhat artifacts (run `npx hardhat compile` first) for both
MockUSDC and FelixVault.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import CONFIG  # noqa: E402

USDC_ARTIFACT = "artifacts/contracts/MockUSDC.sol/MockUSDC.json"
VAULT_ARTIFACT = "artifacts/contracts/FelixVault.sol/FelixVault.json"
USDC_DECIMALS = 6


def _load_artifact(path: str, contract_name: str):
    """Return (abi, bytecode). Prefer Hardhat artifact, else compile with solcx."""
    if os.path.exists(path):
        with open(path) as fh:
            art = json.load(fh)
        bytecode = art["bytecode"]
        if isinstance(bytecode, dict):
            bytecode = bytecode.get("object", "")
        return art["abi"], bytecode

    print(f"   {path} not found — compiling with py-solc-x ...")
    from solcx import compile_standard, install_solc

    install_solc("0.8.20")
    base = os.path.join(os.path.dirname(__file__), "..", "contracts")
    sources = {}
    for rel in [
        "FelixVault.sol",
        "MockUSDC.sol",
        "interfaces/IERC4626.sol",
        "interfaces/IHyperliquidPerp.sol",
    ]:
        fp = os.path.join(base, rel)
        if os.path.exists(fp):
            with open(fp) as fh:
                sources[f"contracts/{rel}"] = {"content": fh.read()}

    compiled = compile_standard(
        {
            "language": "Solidity",
            "sources": sources,
            "settings": {
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
                "optimizer": {"enabled": True, "runs": 200},
            },
        },
        solc_version="0.8.20",
        allow_paths=base,
    )
    src = "contracts/MockUSDC.sol" if contract_name == "MockUSDC" else "contracts/FelixVault.sol"
    c = compiled["contracts"][src][contract_name]
    return c["abi"], c["evm"]["bytecode"]["object"]


def _build_and_send(w3, acct, func, gas: int):
    """Build a legacy (gasPrice) tx from a ContractFunction/constructor and send it.

    gasPrice must be passed INTO build_transaction so web3 builds a legacy tx;
    setting it afterwards conflicts with the auto-added EIP-1559 fields.
    """
    tx = func.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "chainId": CONFIG.CHAIN_ID,
        }
    )
    signed = acct.sign_transaction(tx)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(txh, timeout=180)


def main():
    from web3 import Web3

    if not CONFIG.OPERATOR_PRIVATE_KEY:
        raise SystemExit("OPERATOR_PRIVATE_KEY not set")

    fee_recipient = os.environ.get("FELIX_TREASURY_TESTNET", "")
    operator = os.environ.get("OPERATOR_ADDRESS", "")
    mint_amount = float(os.environ.get("MINT_AMOUNT", "100000"))

    w3 = Web3(Web3.HTTPProvider(CONFIG.HYPEREVM_RPC))
    if not w3.is_connected():
        raise SystemExit(f"Cannot connect to RPC {CONFIG.HYPEREVM_RPC}")

    acct = w3.eth.account.from_key(CONFIG.OPERATOR_PRIVATE_KEY)
    if not operator:
        operator = acct.address
    if not fee_recipient:
        fee_recipient = acct.address

    # 1) Deploy MockUSDC ------------------------------------------------------------
    print("Deploying MockUSDC ...")
    usdc_abi, usdc_bytecode = _load_artifact(USDC_ARTIFACT, "MockUSDC")
    USDC = w3.eth.contract(abi=usdc_abi, bytecode=usdc_bytecode)
    rcpt = _build_and_send(w3, acct, USDC.constructor(), 1_500_000)
    usdc_addr = rcpt.contractAddress
    print(f"   MockUSDC deployed at: {usdc_addr}")

    usdc = w3.eth.contract(address=usdc_addr, abi=usdc_abi)

    # 2) Mint demo USDC to the operator wallet --------------------------------------
    base_units = int(mint_amount * (10**USDC_DECIMALS))
    print(f"Minting {mint_amount:,.0f} USDC to {acct.address} ...")
    _build_and_send(w3, acct, usdc.functions.mint(acct.address, base_units), 200_000)
    bal = usdc.functions.balanceOf(acct.address).call()
    print(f"   Balance: {bal / 10**USDC_DECIMALS:,.2f} USDC")

    # 3) Deploy FelixVault against the MockUSDC -------------------------------------
    print("Deploying FelixVault ...")
    print(f"   asset(USDC):  {usdc_addr}")
    print(f"   feeRecipient: {fee_recipient}")
    print(f"   operator:     {operator}")
    vault_abi, vault_bytecode = _load_artifact(VAULT_ARTIFACT, "FelixVault")
    Vault = w3.eth.contract(abi=vault_abi, bytecode=vault_bytecode)
    rcpt = _build_and_send(
        w3,
        acct,
        Vault.constructor(
            Web3.to_checksum_address(usdc_addr),
            Web3.to_checksum_address(fee_recipient),
            Web3.to_checksum_address(operator),
        ),
        4_000_000,
    )
    vault_addr = rcpt.contractAddress
    print(f"   Vault deployed at: {vault_addr}")

    # 4) Write deployment.json ------------------------------------------------------
    with open("deployment.json", "w") as fh:
        json.dump(
            {
                "address": vault_addr,
                "abi": vault_abi,
                "chainId": CONFIG.CHAIN_ID,
                "rpc": CONFIG.HYPEREVM_RPC,
                "asset": usdc_addr,
                "feeRecipient": fee_recipient,
                "operator": operator,
            },
            fh,
            indent=2,
        )
    print("   Wrote deployment.json")
    print()
    print("=" * 64)
    print("NEXT STEPS")
    print(f"  1. set VAULT_ADDRESS={vault_addr}")
    print(f"  2. Import token {usdc_addr} in MetaMask (USDC, 6 decimals)")
    print(f"     You hold {mint_amount:,.0f} USDC ready to deposit.")
    print("  3. Restart the bot:  python -m bot.operator")
    print("  4. Deposit on the dashboard.")
    print("=" * 64)


if __name__ == "__main__":
    main()
