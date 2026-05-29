"""Felix carry-trade operator bot.

Bridges the on-chain FelixVault contract and Hyperliquid testnet:

  - listens for DeployCapital / UnwindCapital / MarketAdded / MarketRemoved events
  - opens REAL short perps on HL testnet + SIMULATED long spot legs in memory
  - tracks PnL and hedge drift every 15 minutes, rebalancing perp size as needed
  - collects funding every hour, reports it to the vault, and triggers fee accrual

SPOT LEG: SIMULATED (mark-price tracking, no orders) — see spot_simulator.py
PERP LEG: REAL (HL testnet orders)

Both legs reference the same HL mark price, so they cancel; the residual PnL is
pure funding income — the carry-trade yield.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------------
# sys.path hygiene: this module is named `operator`, which collides with Python's stdlib
# `operator` module that dependencies (web3, eth_account, ...) import. If `bot/` is on
# sys.path[0] (as happens with `python bot/operator.py`), those imports would resolve to
# THIS file. We therefore ensure the repo root is on the path and the bot dir is not at
# the front, then import everything as part of the `bot` package.
import os
import sys

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BOT_DIR)
if _BOT_DIR in sys.path:
    sys.path.remove(_BOT_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import asyncio
import json
import logging
import time
from collections import deque
from typing import Deque, Dict, List, Optional

from bot.config import CONFIG
from bot.hyperliquid_client import HyperliquidClient
from bot.spot_simulator import SimulatedSpotTracker
from bot.allocation import Market, get_reliable_markets, compute_allocation

try:
    from web3 import Web3
    _WEB3_AVAILABLE = True
except Exception:  # pragma: no cover
    _WEB3_AVAILABLE = False


# --------------------------------------------------------------------------------------
#                                       LOGGING
# --------------------------------------------------------------------------------------

LOG_RING: Deque[str] = deque(maxlen=200)  # tail buffer the dashboard can read


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_RING.append(self.format(record))
        except Exception:
            pass


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("felix.operator")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    rh = RingHandler()
    rh.setFormatter(fmt)
    logger.addHandler(rh)

    try:
        os.makedirs(os.path.dirname(CONFIG.LOG_FILE) or ".", exist_ok=True)
        fh = logging.FileHandler(CONFIG.LOG_FILE)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


LOG = _setup_logging()


# --------------------------------------------------------------------------------------
#                                     VAULT ABI
# --------------------------------------------------------------------------------------
# Minimal ABI covering exactly the functions + events the bot uses. Must match
# FelixVault.sol. Loaded from the compiled artifact if available, else this fallback.

VAULT_ABI: List[dict] = [
    {"type": "function", "name": "totalAssets", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "sharePrice", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "totalShares", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "pendingFees", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "accumulatedYield", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "activeMarketCount", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "marketId", "stateMutability": "pure", "inputs": [{"name": "coin", "type": "string"}, {"name": "dex", "type": "string"}], "outputs": [{"type": "bytes32"}]},
    {"type": "function", "name": "addMarket", "stateMutability": "nonpayable", "inputs": [{"name": "coin", "type": "string"}, {"name": "dex", "type": "string"}], "outputs": [{"type": "bytes32"}]},
    {"type": "function", "name": "removeMarket", "stateMutability": "nonpayable", "inputs": [{"name": "marketId", "type": "bytes32"}], "outputs": []},
    {"type": "function", "name": "reportFundingYield", "stateMutability": "nonpayable", "inputs": [{"name": "marketId", "type": "bytes32"}, {"name": "yieldAmount", "type": "uint256"}], "outputs": []},
    {"type": "function", "name": "reportPerpSize", "stateMutability": "nonpayable", "inputs": [{"name": "marketId", "type": "bytes32"}, {"name": "perpSize", "type": "int256"}], "outputs": []},
    {"type": "function", "name": "accrueHourlyFees", "stateMutability": "nonpayable", "inputs": [], "outputs": []},
    {"type": "event", "name": "DeployCapital", "anonymous": False, "inputs": [{"name": "marketId", "type": "bytes32", "indexed": True}, {"name": "usdcAmount", "type": "uint256", "indexed": False}]},
    {"type": "event", "name": "UnwindCapital", "anonymous": False, "inputs": [{"name": "marketId", "type": "bytes32", "indexed": True}, {"name": "proportion", "type": "uint256", "indexed": False}]},
    {"type": "event", "name": "MarketAdded", "anonymous": False, "inputs": [{"name": "marketId", "type": "bytes32", "indexed": True}, {"name": "coin", "type": "string", "indexed": False}, {"name": "dex", "type": "string", "indexed": False}]},
    {"type": "event", "name": "MarketRemoved", "anonymous": False, "inputs": [{"name": "marketId", "type": "bytes32", "indexed": True}, {"name": "coin", "type": "string", "indexed": False}, {"name": "dex", "type": "string", "indexed": False}]},
]

USDC_SCALE = 1_000_000  # 6 decimals
PRICE_SCALE = 10**18


def load_vault_abi() -> List[dict]:
    artifact = os.environ.get("VAULT_ARTIFACT", "artifacts/contracts/FelixVault.sol/FelixVault.json")
    try:
        with open(artifact) as fh:
            return json.load(fh)["abi"]
    except Exception:
        return VAULT_ABI


# --------------------------------------------------------------------------------------
#                                    OPERATOR BOT
# --------------------------------------------------------------------------------------


class OperatorBot:
    def __init__(self) -> None:
        self.hl = HyperliquidClient()
        self.spots = SimulatedSpotTracker()
        # market_id -> Market (selection metadata)
        self.markets: Dict[str, Market] = {}
        # market_id -> real perp size (units, positive magnitude of the short)
        self.perp_sizes: Dict[str, float] = {}
        # market_id -> cumulative funding reported to the vault
        self.funding_accum: Dict[str, float] = {}
        self.total_perp_pnl = 0.0
        self.last_funding_check_ms = int(time.time() * 1000)

        self.w3 = None
        self.vault = None
        self.acct = None
        self._connect_chain()

    # --- chain wiring ----------------------------------------------------------------

    def _connect_chain(self) -> None:
        if not _WEB3_AVAILABLE:
            LOG.warning("web3 not installed — running in OFF-CHAIN mode (no vault calls)")
            return
        if not CONFIG.VAULT_ADDRESS:
            LOG.warning("VAULT_ADDRESS not set — running in OFF-CHAIN mode")
            return
        try:
            self.w3 = Web3(Web3.HTTPProvider(CONFIG.HYPEREVM_RPC))
            self.vault = self.w3.eth.contract(
                address=Web3.to_checksum_address(CONFIG.VAULT_ADDRESS),
                abi=load_vault_abi(),
            )
            if CONFIG.OPERATOR_PRIVATE_KEY:
                self.acct = self.w3.eth.account.from_key(CONFIG.OPERATOR_PRIVATE_KEY)
            LOG.info("Connected to vault %s on chain %s", CONFIG.VAULT_ADDRESS, CONFIG.CHAIN_ID)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Chain connection failed (%s) — OFF-CHAIN mode", exc)
            self.w3 = None
            self.vault = None

    def market_id_bytes(self, coin: str, dex: str) -> bytes:
        """keccak256(abi.encodePacked(dex, ":", coin)) — matches the contract."""
        if _WEB3_AVAILABLE:
            return Web3.keccak(text=f"{dex}:{coin}")
        import hashlib  # extremely unlikely fallback
        return hashlib.sha3_256(f"{dex}:{coin}".encode()).digest()

    def _send(self, fn) -> Optional[str]:
        """Build, sign and send a contract transaction; return tx hash or None."""
        if self.vault is None or self.acct is None:
            LOG.info("[off-chain] would send tx: %s", getattr(fn, "fn_name", fn))
            return None
        try:
            tx = fn.build_transaction(
                {
                    "from": self.acct.address,
                    "nonce": self.w3.eth.get_transaction_count(self.acct.address),
                    "gas": 600_000,
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": CONFIG.CHAIN_ID,
                }
            )
            signed = self.acct.sign_transaction(tx)
            txh = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            self.w3.eth.wait_for_transaction_receipt(txh, timeout=120)
            return txh.hex()
        except Exception as exc:  # noqa: BLE001
            LOG.error("tx failed: %s", exc)
            return None

    # --- position execution ----------------------------------------------------------

    async def open_delta_neutral(self, market: Market, usdc_amount: float) -> dict:
        """Open one delta-neutral position: SIMULATED long spot + REAL short perp."""
        mark_price = self.hl.fetch_mark_price(market.coin, market.dex)

        # Half the USDC funds the (simulated) spot leg, half is perp margin.
        position_size = (usdc_amount / 2.0) / mark_price
        mid = market.id

        # 3. SIMULATED SPOT — memory only, NO order.
        self.spots.open_with_id(mid, market.coin, position_size, mark_price, usdc_amount / 2.0)
        LOG.info(
            "SIMULATED SPOT: long %.4f %s @ $%.2f (tracked in memory, no order)",
            position_size, market.coin, mark_price,
        )

        # 4. REAL SHORT PERP on HL testnet.
        order = self.hl.order(
            coin=market.coin,
            is_buy=False,
            sz=position_size,
            limit_px=mark_price * 0.998,
        )
        LOG.info(
            "REAL PERP SHORT: %.4f %s @ $%.2f on HL testnet (%s)",
            position_size, market.coin, mark_price,
            "sim" if order.get("simulated") else "live",
        )

        # 5. Verify the perp filled; never carry unhedged simulated exposure.
        if order["status"] not in ("ok",) or order["filled_size"] <= 0:
            LOG.error("PERP FILL FAILED for %s — removing simulated spot to stay flat", market.coin)
            self.spots.close(mid, 1.0, mark_price)
            return {"coin": market.coin, "status": "failed"}

        filled = order["filled_size"]
        self.perp_sizes[mid] = self.perp_sizes.get(mid, 0.0) + filled
        self.markets[mid] = market
        self.funding_accum.setdefault(mid, 0.0)

        # 6. Tell the vault the real perp size (negative = short, scaled 1e8).
        self._report_perp_size(market, -self.perp_sizes[mid])

        delta = self._delta(mid, mark_price)
        LOG.info("OPENED %s | delta %.6f (target ~0)", market.coin, delta)
        return {
            "coin": market.coin,
            "status": "ok",
            "entry_price": mark_price,
            "spot_size": position_size,
            "perp_size": filled,
            "delta": delta,
        }

    async def close_delta_neutral(self, market_id: str, proportion: float) -> dict:
        """Close `proportion` (0..1) of a position in one market."""
        market = self.markets.get(market_id)
        spot = self.spots.get(market_id)
        if market is None or spot is None:
            LOG.warning("close requested for unknown market %s", market_id)
            return {"status": "unknown"}

        proportion = max(0.0, min(1.0, proportion))
        current_price = self.hl.fetch_mark_price(market.coin, market.dex)

        # 1-2. Realise the simulated spot PnL on the closing slice.
        closing_size = spot.size * proportion
        usdc_freed = spot.usdc_allocated * proportion
        spot_pnl = self.spots.close(market_id, proportion, current_price)
        LOG.info(
            "CLOSING SIMULATED SPOT: %.4f %s entry $%.2f -> exit $%.2f PnL $%.2f",
            closing_size, market.coin, spot.entry_price, current_price, spot_pnl,
        )

        # 3. Close the REAL perp short proportionally (buy to close).
        perp_close = self.perp_sizes.get(market_id, 0.0) * proportion
        order = self.hl.order(
            coin=market.coin,
            is_buy=True,
            sz=perp_close,
            limit_px=current_price * 1.002,
        )
        LOG.info("CLOSED REAL PERP SHORT: %.4f %s", perp_close, market.coin)

        perp_pnl = self.hl.get_position_pnl(market.coin)
        self.perp_sizes[market_id] = max(0.0, self.perp_sizes.get(market_id, 0.0) - perp_close)

        # 5. USDC returned = freed margin + spot pnl + perp pnl + accrued funding slice.
        funding_slice = self.funding_accum.get(market_id, 0.0) * proportion
        usdc_received = usdc_freed + spot_pnl + perp_pnl * proportion + funding_slice

        # 6. Report the unwind to the vault as realised yield (only the funding component
        #    is genuine yield; spot+perp ≈ 0 by construction).
        if funding_slice > 0:
            self._report_funding(market, funding_slice)
            self.funding_accum[market_id] -= funding_slice

        if self.perp_sizes.get(market_id, 0.0) <= 1e-9:
            self.perp_sizes.pop(market_id, None)
            self._report_perp_size(market, 0)
        else:
            self._report_perp_size(market, -self.perp_sizes[market_id])

        LOG.info(
            "UNWOUND %s prop %.2f | freed $%.2f + spot $%.2f + perp $%.2f + funding $%.2f = $%.2f",
            market.coin, proportion, usdc_freed, spot_pnl, perp_pnl * proportion, funding_slice, usdc_received,
        )
        return {"status": "ok", "usdc_received": usdc_received, "spot_pnl": spot_pnl, "perp_pnl": perp_pnl}

    # --- monitoring ------------------------------------------------------------------

    def _delta(self, market_id: str, price: float) -> float:
        spot = self.spots.get(market_id)
        if spot is None or spot.size == 0:
            return 0.0
        spot_notional = spot.size * price
        perp_notional = self.perp_sizes.get(market_id, 0.0) * price
        return (spot_notional - perp_notional) / spot_notional if spot_notional else 0.0

    async def track_pnl(self) -> None:
        """Every 15 min: mark spot, fetch perp PnL + funding, check hedge drift."""
        for market_id, market in list(self.markets.items()):
            spot = self.spots.get(market_id)
            if spot is None:
                continue
            try:
                price = self.hl.fetch_mark_price(market.coin, market.dex)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("price fetch failed for %s: %s", market.coin, exc)
                continue

            spot_pnl = self.spots.get_pnl(market_id, price)
            perp_pnl = self.hl.get_position_pnl(market.coin)
            funding = self.funding_accum.get(market_id, 0.0)
            net = spot_pnl + perp_pnl + funding

            LOG.info(
                "[PnL] %s: spot $%+.2f | perp $%+.2f | funding $%+.2f | net $%+.2f",
                market.coin, spot_pnl, perp_pnl, funding, net,
            )

            hedge_drift = abs(spot_pnl + perp_pnl)
            if hedge_drift > spot.usdc_allocated * CONFIG.HEDGE_DRIFT_WARN_PCT:
                LOG.warning(
                    "WARNING: %s hedge drift $%.2f exceeds %.0f%% threshold",
                    market.coin, hedge_drift, CONFIG.HEDGE_DRIFT_WARN_PCT * 100,
                )

    async def check_and_rebalance(self) -> None:
        """Every 15 min: resize the perp if it has drifted >REBALANCE_THRESHOLD from spot notional."""
        for market_id, market in list(self.markets.items()):
            spot = self.spots.get(market_id)
            if spot is None:
                continue
            try:
                price = self.hl.fetch_mark_price(market.coin, market.dex)
            except Exception:
                continue

            delta = self._delta(market_id, price)
            if abs(delta) <= CONFIG.REBALANCE_THRESHOLD:
                continue

            spot_notional = spot.size * price
            target_perp_units = spot_notional / price  # == spot.size
            current_perp = self.perp_sizes.get(market_id, 0.0)
            adjust = target_perp_units - current_perp
            LOG.info("Rebalancing %s: delta was %.2f%% — adjusting perp by %.4f units",
                     market.coin, delta * 100, adjust)

            if abs(adjust) < 1e-9:
                continue
            is_buy = adjust < 0  # need to reduce short -> buy; increase short -> sell
            order = self.hl.order(
                coin=market.coin,
                is_buy=is_buy,
                sz=abs(adjust),
                limit_px=price * (1.002 if is_buy else 0.998),
            )
            if order["status"] == "ok":
                self.perp_sizes[market_id] = current_perp + (order["filled_size"] if not is_buy else -order["filled_size"])
                self._report_perp_size(market, -self.perp_sizes[market_id])

    async def collect_and_report_funding(self) -> None:
        """Every hour: pull funding per market, report to vault, accrue fees, log summary."""
        since_ms = self.last_funding_check_ms
        self.last_funding_check_ms = int(time.time() * 1000)

        total_funding = 0.0
        for market_id, market in list(self.markets.items()):
            amount = self.hl.get_funding_received(market.coin, since_ms)
            if amount <= 0:
                continue
            self.funding_accum[market_id] = self.funding_accum.get(market_id, 0.0) + amount
            total_funding += amount
            self._report_funding(market, amount)
            LOG.info("%s: received $%.2f funding this hour (%.1f%% APY annualised)",
                     market.coin, amount, market.funding_apy)

        # Trigger hourly fee accrual on the vault.
        self._accrue_fees()

        # Protocol summary.
        fees = total_funding * 0.08  # 8% performance fee
        net = total_funding - fees
        sim_pnl = self.spots.total_unrealised_pnl()
        perp_pnl = sum(self.hl.get_position_pnl(m.coin) for m in self.markets.values())
        LOG.info("=== HOURLY PROTOCOL SUMMARY ===")
        LOG.info("Total funding collected: $%.2f", total_funding)
        LOG.info("Fees accrued (8%%): $%.2f", fees)
        LOG.info("Net depositor yield: $%.2f", net)
        LOG.info("Current share price: $%.6f", self._share_price())
        LOG.info("Simulated spot total PnL: $%.2f (should be near zero)", sim_pnl)
        LOG.info("Real perp total PnL: $%.2f (should be near zero)", perp_pnl)
        LOG.info("Net hedge drift: $%.2f (spot + perp, should be ~0)", sim_pnl + perp_pnl)
        self._write_state()

    # --- vault call helpers ----------------------------------------------------------

    def _report_funding(self, market: Market, amount: float) -> None:
        if self.vault is None:
            return
        mid = self.market_id_bytes(market.coin, market.dex)
        self._send(self.vault.functions.reportFundingYield(mid, int(amount * USDC_SCALE)))

    def _report_perp_size(self, market: Market, size_units: float) -> None:
        if self.vault is None:
            return
        mid = self.market_id_bytes(market.coin, market.dex)
        self._send(self.vault.functions.reportPerpSize(mid, int(size_units * 1e8)))

    def _accrue_fees(self) -> None:
        if self.vault is None:
            return
        self._send(self.vault.functions.accrueHourlyFees())

    def _share_price(self) -> float:
        if self.vault is None:
            return 1.0
        try:
            return self.vault.functions.sharePrice().call() / PRICE_SCALE
        except Exception:
            return 1.0

    def _add_market_onchain(self, market: Market) -> None:
        if self.vault is None:
            return
        self._send(self.vault.functions.addMarket(market.coin, market.dex))

    # --- market list -----------------------------------------------------------------

    async def refresh_market_list(self) -> List[Market]:
        markets = await get_reliable_markets(self.hl)
        LOG.info("Refreshed reliable markets: %d", len(markets))
        for m in markets:
            if m.id not in self.markets:
                self._add_market_onchain(m)
                self.markets[m.id] = m
        return markets

    # --- event listener --------------------------------------------------------------

    async def listen_for_vault_events(self) -> None:
        """Poll vault logs via get_logs (HyperEVM doesn't support eth_newFilter)."""
        if self.vault is None:
            LOG.warning("event listener disabled (off-chain mode)")
            return

        from_block: int = self.w3.eth.block_number
        LOG.info("Listening for vault events from block %s (get_logs polling)", from_block)

        deploy_topic = self.vault.events.DeployCapital._get_event_abi()
        unwind_topic = self.vault.events.UnwindCapital._get_event_abi()

        while True:
            await asyncio.sleep(5)
            try:
                to_block = self.w3.eth.block_number
                if to_block < from_block:
                    continue
                raw_logs = self.w3.eth.get_logs({
                    "address": self.vault.address,
                    "fromBlock": from_block,
                    "toBlock": to_block,
                })
                for raw in raw_logs:
                    try:
                        try:
                            ev = self.vault.events.DeployCapital().process_log(raw)
                            await self._on_deploy(ev)
                            continue
                        except Exception:
                            pass
                        try:
                            ev = self.vault.events.UnwindCapital().process_log(raw)
                            await self._on_unwind(ev)
                            continue
                        except Exception:
                            pass
                        try:
                            ev = self.vault.events.MarketAdded().process_log(raw)
                            LOG.info("MarketAdded: %s (%s)", ev["args"]["coin"], ev["args"]["dex"])
                            continue
                        except Exception:
                            pass
                        try:
                            ev = self.vault.events.MarketRemoved().process_log(raw)
                            LOG.info("MarketRemoved: %s (%s)", ev["args"]["coin"], ev["args"]["dex"])
                        except Exception:
                            pass
                    except Exception as exc:  # noqa: BLE001
                        LOG.debug("log decode error: %s", exc)
                from_block = to_block + 1
            except Exception as exc:  # noqa: BLE001
                LOG.error("event poll error: %s", exc)

    def _market_by_id_bytes(self, market_id_bytes: bytes) -> Optional[Market]:
        for m in self.markets.values():
            if self.market_id_bytes(m.coin, m.dex) == market_id_bytes:
                return m
        return None

    async def _on_deploy(self, ev) -> None:
        mid_bytes = ev["args"]["marketId"]
        amount = ev["args"]["usdcAmount"] / USDC_SCALE
        market = self._market_by_id_bytes(mid_bytes)
        if market is None:
            LOG.warning("DeployCapital for unknown market %s", mid_bytes.hex())
            return
        LOG.info("DeployCapital -> %s $%.2f", market.coin, amount)
        await self.open_delta_neutral(market, amount)

    async def _on_unwind(self, ev) -> None:
        mid_bytes = ev["args"]["marketId"]
        proportion = ev["args"]["proportion"] / PRICE_SCALE
        market = self._market_by_id_bytes(mid_bytes)
        if market is None:
            return
        LOG.info("UnwindCapital -> %s prop %.4f", market.coin, proportion)
        await self.close_delta_neutral(market.id, proportion)

    # --- scheduled task runner -------------------------------------------------------

    async def _every(self, interval: int, coro_fn, name: str) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await coro_fn()
            except Exception as exc:  # noqa: BLE001
                LOG.error("scheduled task %s failed: %s", name, exc)

    async def run(self) -> None:
        LOG.info("=" * 60)
        LOG.info("FELIX VAULT OPERATOR BOT starting")
        LOG.info("Spot leg: SIMULATED (mark price tracking)")
        LOG.info("Perp leg: REAL (HL testnet positions)")
        LOG.info("HL API: %s | RPC: %s", CONFIG.HL_TESTNET_API, CONFIG.HYPEREVM_RPC)
        LOG.info("Operator HL address: %s", self.hl.address or "(dry-run / none)")
        LOG.info("=" * 60)

        markets = await self.refresh_market_list()
        for m in markets[:10]:
            LOG.info("  %s (%s): %.1f%% APY, $%.2fM capacity",
                     m.coin, m.dex, m.funding_apy, m.capacity / 1e6)

        LOG.info("Total reliable markets: %d", len(markets))
        LOG.info("Current share price: $%.6f", self._share_price())

        tasks = [
            asyncio.create_task(self.listen_for_vault_events()),
            asyncio.create_task(self._every(CONFIG.PNL_TRACK_INTERVAL, self._pnl_and_rebalance, "pnl+rebalance")),
            asyncio.create_task(self._every(CONFIG.FUNDING_REPORT_INTERVAL, self.collect_and_report_funding, "funding")),
            asyncio.create_task(self._every(CONFIG.MARKET_REFRESH_INTERVAL, self.refresh_market_list, "market-refresh")),
        ]
        await asyncio.gather(*tasks)

    async def _pnl_and_rebalance(self) -> None:
        await self.track_pnl()
        await self.check_and_rebalance()
        self._write_state()

    # --- dashboard state dump --------------------------------------------------------

    def _write_state(self) -> None:
        """Publish a JSON snapshot the dashboard can read (sim-spot data lives only here).

        Written to bot/state.json. The dashboard reads on-chain data via ethers directly;
        this file supplies the off-chain pieces (simulated spot legs, hedge drift, log).
        """
        markets_out = []
        total_sim = 0.0
        total_perp = 0.0
        total_funding = 0.0
        for market_id, market in self.markets.items():
            spot = self.spots.get(market_id)
            if spot is None:
                continue
            try:
                price = self.hl.fetch_mark_price(market.coin, market.dex)
            except Exception:
                price = spot.current_price
            spot_pnl = self.spots.get_pnl(market_id, price)
            perp_pnl = self.hl.get_position_pnl(market.coin)
            funding = self.funding_accum.get(market_id, 0.0)
            perp_size = self.perp_sizes.get(market_id, 0.0)
            total_sim += spot_pnl
            total_perp += perp_pnl
            total_funding += funding
            markets_out.append({
                "id": market_id,
                "coin": market.coin,
                "dex": market.dex,
                "allocated_usdc": spot.usdc_allocated * 2,
                "spot_size": spot.size,
                "spot_pnl": spot_pnl,
                "perp_size": -perp_size,
                "perp_pnl": perp_pnl,
                "funding": funding,
                "net_pnl": spot_pnl + perp_pnl + funding,
                "apy": market.funding_apy,
                "current_price": price,
                "entry_price": spot.entry_price,
            })

        drift = total_sim + total_perp
        state = {
            "updated_at": int(time.time()),
            "share_price": self._share_price(),
            "total_funding": total_funding,
            "total_sim_pnl": total_sim,
            "total_perp_pnl": total_perp,
            "hedge_drift": drift,
            "hedge_status": "neutral" if abs(drift) < max(1.0, 0.01 * sum(m["allocated_usdc"] for m in markets_out)) else "drift",
            "markets": markets_out,
            "log_tail": list(LOG_RING)[-50:],
        }
        try:
            path = os.path.join(_BOT_DIR, "state.json")
            with open(path, "w") as fh:
                json.dump(state, fh, indent=2)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("state write failed: %s", exc)


async def main() -> None:
    bot = OperatorBot()
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.info("operator bot stopped")
