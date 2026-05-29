"""Thin wrapper around the Hyperliquid testnet API.

Responsibilities:
  - read-only market data (meta, asset contexts, mark prices, funding history)
  - placing / closing REAL short perp orders on testnet (via hyperliquid-python-sdk)
  - reading perp position PnL and funding received

Everything points at testnet. When `DRY_RUN` is set, or when the SDK / signing key
is unavailable, order methods log and return a simulated fill instead of hitting the
network, so the bot can be exercised end-to-end without funds.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

try:
    from bot.config import CONFIG
except ImportError:  # when bot/ is on sys.path directly
    from config import CONFIG

# The official SDK is optional at import time so the module can be imported in
# environments where it isn't installed (e.g. CI lint). Real order placement
# requires it.
try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account

    _SDK_AVAILABLE = True
except Exception:  # pragma: no cover - SDK optional
    _SDK_AVAILABLE = False


class HyperliquidClient:
    def __init__(self, private_key: Optional[str] = None, dry_run: Optional[bool] = None):
        self.base_url = CONFIG.HL_TESTNET_API
        self.dry_run = CONFIG.DRY_RUN if dry_run is None else dry_run
        self._private_key = private_key or CONFIG.OPERATOR_PRIVATE_KEY
        self._info = None
        self._exchange = None
        self._address = None
        self._coin_to_dex: Dict[str, str] = {}

        if _SDK_AVAILABLE:
            try:
                self._info = Info(constants.TESTNET_API_URL, skip_ws=True)
            except Exception:
                self._info = None
            if self._private_key and not self.dry_run:
                try:
                    wallet = Account.from_key(self._private_key)
                    self._address = wallet.address
                    self._exchange = Exchange(
                        wallet, constants.TESTNET_API_URL
                    )
                except Exception:
                    self._exchange = None

    # ----------------------------------------------------------------------------------
    #                                   READ-ONLY INFO
    # ----------------------------------------------------------------------------------

    def _post_info(self, body: dict) -> object:
        """POST to the /info endpoint with a small retry."""
        url = f"{self.base_url}/info"
        last_exc = None
        for attempt in range(3):
            try:
                resp = requests.post(url, json=body, timeout=10)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"HL info request failed: {body.get('type')}: {last_exc}")

    def meta_and_asset_ctxs(self, dex: str = "") -> List:
        """Return [universe, contexts] for a HIP-3 dex (or main if dex empty)."""
        body: Dict[str, object] = {"type": "metaAndAssetCtxs"}
        if dex:
            body["dex"] = dex
        return self._post_info(body)

    def fetch_mark_price(self, coin: str, dex: str = "") -> float:
        """Current mark price for `coin` on its dex."""
        dex = dex or self._coin_to_dex.get(coin, "")
        universe, ctxs = self.meta_and_asset_ctxs(dex)
        names = [u["name"] for u in universe]
        if coin not in names:
            raise KeyError(f"coin {coin!r} not found on dex {dex!r}")
        idx = names.index(coin)
        markpx = ctxs[idx].get("markPx") or ctxs[idx].get("oraclePx")
        return float(markpx)

    def funding_history(self, coin: str, start_ms: int) -> List[dict]:
        body = {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
        try:
            return self._post_info(body) or []
        except Exception:
            return []

    def user_funding_history(self, address: str, start_ms: int) -> List[dict]:
        body = {"type": "userFunding", "user": address, "startTime": start_ms}
        try:
            return self._post_info(body) or []
        except Exception:
            return []

    def user_state(self, address: Optional[str] = None) -> dict:
        address = address or self._address
        if not address:
            return {}
        body = {"type": "clearinghouseState", "user": address}
        try:
            return self._post_info(body) or {}
        except Exception:
            return {}

    def remember_dex(self, coin: str, dex: str) -> None:
        """Cache the dex for a coin so later lookups don't need it passed explicitly."""
        self._coin_to_dex[coin] = dex

    # ----------------------------------------------------------------------------------
    #                                  POSITION QUERIES
    # ----------------------------------------------------------------------------------

    def get_position_pnl(self, coin: str) -> float:
        """Unrealised PnL of the short perp for `coin` (USDC). 0 if no position."""
        state = self.user_state()
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                try:
                    return float(pos.get("unrealizedPnl", 0.0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def get_funding_received(self, coin: str, since_ms: Optional[int] = None) -> float:
        """Sum funding payments received for `coin` since `since_ms` (default 1h ago).

        Hyperliquid signs funding so that a short collecting funding shows up as a
        positive delta to the account. We sum the `usdc` deltas for this coin.
        """
        if not self._address:
            return 0.0
        since_ms = since_ms or int((time.time() - 3600) * 1000)
        total = 0.0
        for ev in self.user_funding_history(self._address, since_ms):
            delta = ev.get("delta", {})
            if delta.get("coin") == coin:
                try:
                    total += float(delta.get("usdc", 0.0))
                except (TypeError, ValueError):
                    continue
        return total

    # ----------------------------------------------------------------------------------
    #                                   ORDER PLACEMENT
    # ----------------------------------------------------------------------------------

    def order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        tif: str = "Ioc",
    ) -> dict:
        """Place a REAL perp order on testnet (IOC limit by default).

        Returns a dict {status, filled_size, avg_px, raw}. In dry-run / no-SDK mode
        returns a simulated full fill at `limit_px`.
        """
        side = "BUY" if is_buy else "SELL"
        if self.dry_run or self._exchange is None:
            return {
                "status": "ok",
                "filled_size": sz,
                "avg_px": limit_px,
                "simulated": True,
                "side": side,
                "coin": coin,
                "raw": None,
            }
        try:
            result = self._exchange.order(
                coin,
                is_buy,
                float(sz),
                float(limit_px),
                {"limit": {"tif": tif}},
            )
            filled = self._extract_fill(result)
            return {
                "status": "ok" if filled["sz"] > 0 else "unfilled",
                "filled_size": filled["sz"],
                "avg_px": filled["px"] or limit_px,
                "simulated": False,
                "side": side,
                "coin": coin,
                "raw": result,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "filled_size": 0.0,
                "avg_px": 0.0,
                "simulated": False,
                "side": side,
                "coin": coin,
                "error": str(exc),
            }

    @staticmethod
    def _extract_fill(result: dict) -> dict:
        """Pull total filled size and avg price out of an SDK order response."""
        sz = 0.0
        px = 0.0
        try:
            statuses = result["response"]["data"]["statuses"]
            for s in statuses:
                if "filled" in s:
                    f = s["filled"]
                    sz += float(f.get("totalSz", 0.0))
                    px = float(f.get("avgPx", 0.0))
        except Exception:
            pass
        return {"sz": sz, "px": px}

    @property
    def address(self) -> Optional[str]:
        return self._address

    @property
    def ready_for_orders(self) -> bool:
        return self._exchange is not None and not self.dry_run
