"""End-to-end demonstration of the Felix vault flow on HyperEVM + HL testnet.

    python scripts/test_full_flow.py

What it does:
  1. Deploys (or reuses) FelixVault
  2. Loads reliable HIP-3 markets from HL testnet
  3. Deposits $10,000 USDC
  4. Shows the greedy allocation
  5. Opens delta-neutral positions (SIMULATED spot + REAL perp)
  6. Waits for funding, collects + reports it, shows hedge health
  7. Withdraws 50% and shows the performance fee
  8. Prints a final summary

Set FAST_DEMO=1 to skip the real 1-hour funding wait (uses a short sleep) so the
flow can be demonstrated quickly. With FAST_DEMO unset it waits a real hour.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Add the repo root so the `bot` package is importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import CONFIG  # noqa: E402
from bot.allocation import get_reliable_markets, compute_allocation  # noqa: E402


async def run_test():
    print("=== FELIX VAULT TESTNET DEMO ===")
    print("Spot leg: SIMULATED (HL mark price)")
    print("Perp leg: REAL (HL testnet orders)\n")

    from bot.operator import OperatorBot  # imported lazily so HL/web3 init is deferred

    bot = OperatorBot()
    vault = bot.vault  # may be None in off-chain demo mode

    # 1. DEPLOY (informational — assumes deploy.py was run, or VAULT_ADDRESS set)
    print("1. FelixVault")
    if vault is not None:
        print(f"   Using deployed vault: {CONFIG.VAULT_ADDRESS}")
    else:
        print("   OFF-CHAIN demo mode (no VAULT_ADDRESS / web3) — accounting is simulated")

    # 2. LOAD MARKETS
    print("\n2. Loading reliable HIP-3 markets...")
    markets = await get_reliable_markets(bot.hl)
    print(f"   Found {len(markets)} reliable markets:")
    for m in markets[:10]:
        print(f"   {m.coin} ({m.dex}): {m.funding_apy:.1f}% APY, ${m.capacity / 1e6:.2f}M capacity")
    for m in markets:
        bot.markets[m.id] = m

    if not markets:
        print("   No reliable markets right now — funding may be soft. Exiting demo.")
        return

    deposit_usdc = 10_000.0

    # 3. DEPOSIT
    print(f"\n3. Simulating deposit of ${deposit_usdc:,.0f} USDC...")
    if vault is not None:
        print("   (deposit() should be called by a funded depositor wallet via the dashboard)")
    print(f"   Share price: ${bot._share_price():.6f}")

    # 4. SHOW ALLOCATION
    print("\n4. Capital allocated across markets:")
    allocation = await compute_allocation(deposit_usdc, markets)
    for market_id, amount in allocation.items():
        m = bot.markets[market_id]
        print(f"   {m.coin}: ${amount:,.2f} "
              f"(${amount / 2:,.2f} sim spot + ${amount / 2:,.2f} perp margin)")

    # 5. OPEN POSITIONS
    print("\n5. Opening delta-neutral positions...")
    for market_id, amount in allocation.items():
        m = bot.markets[market_id]
        pos = await bot.open_delta_neutral(m, amount)
        if pos.get("status") != "ok":
            print(f"   {m.coin}: open FAILED, skipped")
            continue
        print(f"   {pos['coin']}:")
        print(f"     Sim spot: long {pos['spot_size']:.4f} @ ${pos['entry_price']:.2f} (in memory)")
        print(f"     Real perp: short {pos['perp_size']:.4f} @ ${pos['entry_price']:.2f} (HL testnet)")
        print(f"     Delta: {pos['delta']:.6f} (should be ~0)")

    # 6. WAIT AND COLLECT FUNDING
    wait_s = 8 if os.environ.get("FAST_DEMO") else 3600
    print(f"\n6. Waiting {wait_s}s for funding collection...")
    await asyncio.sleep(wait_s)
    await bot.collect_and_report_funding()
    total_funding = sum(bot.funding_accum.values())
    print(f"   Funding collected: ${total_funding:.2f}")
    print(f"   Felix fee (8%): ${total_funding * 0.08:.2f}")
    print(f"   Depositor yield: ${total_funding * 0.92:.2f}")

    # Hedge health
    print("   Hedge health:")
    for market_id, market in bot.markets.items():
        spot = bot.spots.get(market_id)
        if spot is None:
            continue
        perp_pnl = bot.hl.get_position_pnl(market.coin)
        print(f"   {market.coin}: spot PnL ${spot.unrealised_pnl:+.2f} + perp PnL ${perp_pnl:+.2f} "
              f"= drift ${spot.unrealised_pnl + perp_pnl:+.2f}")

    print(f"   New share price: ${bot._share_price():.6f}")

    # 7. WITHDRAW 50%
    print("\n7. Withdrawing 50% of position...")
    for market_id in list(bot.markets.keys()):
        if bot.spots.get(market_id) is not None:
            await bot.close_delta_neutral(market_id, 0.5)
    print("   Closed 50% of every market's perp + simulated spot")
    if vault is not None:
        print("   (vault.withdraw(shares/2) should be called by the depositor wallet)")

    # 8. SUMMARY
    print("\n=== TEST COMPLETE ===")
    total_sim = bot.spots.total_unrealised_pnl()
    total_perp = sum(bot.hl.get_position_pnl(m.coin) for m in bot.markets.values())
    print(f"Active markets: {len(bot.markets)}")
    print(f"Share price: ${bot._share_price():.6f}")
    print(f"Total sim spot PnL: ${total_sim:.2f}")
    print(f"Total real perp PnL: ${total_perp:.2f}")
    print(f"Net hedge drift: ${total_sim + total_perp:.2f} (should be ~$0)")
    print(f"Funding remaining (post 50% unwind): ${sum(bot.funding_accum.values()):.2f}")


if __name__ == "__main__":
    asyncio.run(run_test())
