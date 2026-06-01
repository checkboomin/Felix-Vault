"""Deposit mock USDC into FelixVault directly (no MetaMask).

Usage:
    python scripts/deposit.py            # deposits DEPOSIT_AMOUNT or 100000
    python scripts/deposit.py 50000      # deposits 50,000 USDC

Uses OPERATOR_PRIVATE_KEY (the wallet that holds the minted mock USDC) to
approve + deposit straight on-chain. Reads vault address + asset from
deployment.json. Avoids browser/MetaMask network + decode issues.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import CONFIG  # noqa: E402

USDC_DECIMALS = 6
ERC20_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
]


def _retry(fn, tries: int = 8, delay: float = 2.0):
    """Call fn(), retrying on transient RPC errors (rate limited, etc.)."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            msg = str(exc).lower()
            if "rate limit" in msg or "-32005" in msg or "timeout" in msg or "too many" in msg:
                time.sleep(delay * (i + 1))
                continue
            raise
    raise last


def _wait_receipt(w3, txh):
    """Poll for a receipt manually, tolerating rate-limit errors."""
    for i in range(40):
        rcpt = _retry(lambda: w3.eth.get_transaction_receipt(txh) if _exists(w3, txh) else None)
        if rcpt is not None:
            return rcpt
        time.sleep(3)
    raise SystemExit("Timed out waiting for receipt (tx may still confirm — check the dashboard)")


def _exists(w3, txh):
    try:
        return w3.eth.get_transaction_receipt(txh) is not None
    except Exception:
        return False


def _send(w3, acct, func, gas: int):
    nonce = _retry(lambda: w3.eth.get_transaction_count(acct.address))
    gas_price = _retry(lambda: w3.eth.gas_price)
    tx = func.build_transaction({
        "from": acct.address,
        "nonce": nonce,
        "gas": gas,
        "gasPrice": gas_price,
        "chainId": CONFIG.CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    txh = _retry(lambda: w3.eth.send_raw_transaction(signed.raw_transaction))
    return _wait_receipt(w3, txh)


def main():
    from web3 import Web3

    amount = float(sys.argv[1]) if len(sys.argv) > 1 else float(os.environ.get("DEPOSIT_AMOUNT", "100000"))

    if not CONFIG.OPERATOR_PRIVATE_KEY:
        raise SystemExit("OPERATOR_PRIVATE_KEY not set")
    if not os.path.exists("deployment.json"):
        raise SystemExit("deployment.json not found — deploy first")

    with open("deployment.json") as fh:
        dep = json.load(fh)
    vault_addr = Web3.to_checksum_address(dep["address"])
    asset_addr = Web3.to_checksum_address(dep["asset"])
    vault_abi = dep["abi"]

    w3 = Web3(Web3.HTTPProvider(CONFIG.HYPEREVM_RPC))
    if not w3.is_connected():
        raise SystemExit(f"Cannot connect to RPC {CONFIG.HYPEREVM_RPC}")
    acct = w3.eth.account.from_key(CONFIG.OPERATOR_PRIVATE_KEY)

    usdc = w3.eth.contract(address=asset_addr, abi=ERC20_ABI)
    vault = w3.eth.contract(address=vault_addr, abi=vault_abi)

    base = int(amount * (10**USDC_DECIMALS))
    bal = _retry(lambda: usdc.functions.balanceOf(acct.address).call())
    shares_now = _retry(lambda: vault.functions.shares(acct.address).call())
    tvl_now = _retry(lambda: vault.functions.totalAssets().call())
    print(f"Wallet:        {acct.address}")
    print(f"USDC balance:  {bal / 10**USDC_DECIMALS:,.2f}")
    print(f"Vault:         {vault_addr}")
    print(f"Current TVL:   {tvl_now / 10**USDC_DECIMALS:,.2f} USDC | your shares: {shares_now / 10**USDC_DECIMALS:,.2f}")

    # Idempotency: if a prior run already deposited (shares minted, balance spent), stop.
    if shares_now > 0 and bal < base:
        print("\nLooks like your deposit already went through (shares minted, USDC spent).")
        print("Nothing more to do — watch the bot window / dashboard for yield.")
        return
    if bal < base:
        raise SystemExit(f"Insufficient USDC: have {bal/1e6:,.2f}, need {amount:,.2f}")

    print(f"Depositing:    {amount:,.2f} USDC")
    allowance = _retry(lambda: usdc.functions.allowance(acct.address, vault_addr).call())
    if allowance < base:
        print("Approving vault to spend USDC ...")
        _send(w3, acct, usdc.functions.approve(vault_addr, base), 120_000)
        print("   approved")

    print("Depositing ...")
    rcpt = _send(w3, acct, vault.functions.deposit(base), 3_000_000)
    print(f"   deposit tx: {rcpt.transactionHash.hex()}  (status {rcpt.status})")

    try:
        shares = _retry(lambda: vault.functions.shares(acct.address).call())
        ta = _retry(lambda: vault.functions.totalAssets().call())
        print(f"   your shares:  {shares / 10**USDC_DECIMALS:,.2f}")
        print(f"   vault TVL:    {ta / 10**USDC_DECIMALS:,.2f} USDC")
    except Exception:
        pass
    print("Done. Watch the bot window for DeployCapital + funding accrual.")


if __name__ == "__main__":
    main()
