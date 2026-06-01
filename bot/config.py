"""Configuration for the Felix carry-trade operator bot.

All secrets are read from the environment (see .env.example). Nothing here should
contain real keys. Everything points at Hyperliquid *testnet* by default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    # Optional: load a local .env if python-dotenv is installed.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    # --- Endpoints -------------------------------------------------------------------
    HL_TESTNET_API: str = _get("HL_TESTNET_API", "https://api.hyperliquid-testnet.xyz")
    # Read-only market data (funding rates, OI, mark prices). Defaults to MAINNET so the
    # demo uses real, non-zero HIP-3 funding while execution stays on testnet.
    HL_DATA_API: str = _get("HL_DATA_API", "https://api.hyperliquid.xyz")
    HYPEREVM_RPC: str = _get("HYPEREVM_RPC", "https://rpc.hyperliquid-testnet.xyz/evm")
    CHAIN_ID: int = int(_get("CHAIN_ID", "998"))

    # --- On-chain wiring -------------------------------------------------------------
    VAULT_ADDRESS: str = _get("VAULT_ADDRESS", "")
    OPERATOR_PRIVATE_KEY: str = _get("OPERATOR_PRIVATE_KEY", "")
    FELIX_WALLET: str = _get("FELIX_WALLET", "")

    # --- HIP-3 dexes to consider (matches the capacity dashboard) --------------------
    HIP3_DEXES: List[str] = field(
        default_factory=lambda: ["xyz", "vntl", "hyna", "km", "cash", "para"]
    )

    # --- Strategy parameters ---------------------------------------------------------
    MIN_VIABLE_APY: float = float(_get("MIN_VIABLE_APY", "9.0"))
    MAX_POSITION_SIZE_PCT: float = float(_get("MAX_POSITION_SIZE_PCT", "0.45"))
    # Cap any single market at this fraction of total vault TVL.
    MAX_TVL_PCT_PER_MARKET: float = float(_get("MAX_TVL_PCT_PER_MARKET", "0.30"))
    REBALANCE_THRESHOLD: float = float(_get("REBALANCE_THRESHOLD", "0.05"))
    MIN_ALLOCATION_USDC: float = float(_get("MIN_ALLOCATION_USDC", "1000"))
    # Cap how many top-APY markets are registered/used on-chain (keeps tx count sane).
    MAX_ACTIVE_MARKETS: int = int(_get("MAX_ACTIVE_MARKETS", "6"))
    # Rank markets by sustained "mean_apy" (30d, matches the capacity dashboard) or
    # momentary "live_apy". Sustained avoids chasing volatile funding spikes.
    RANK_BY: str = _get("RANK_BY", "mean_apy")

    # --- Reliability gate (matches dashboard "Reliable" definition) ------------------
    MAX_CV: float = float(_get("MAX_CV", "0.75"))
    MIN_PCT_TIME_POSITIVE: float = float(_get("MIN_PCT_TIME_POSITIVE", "80.0"))
    MIN_PCT_TIME_ABOVE_9: float = float(_get("MIN_PCT_TIME_ABOVE_9", "60.0"))
    # Comma-separated list of accepted ratings, e.g. "Reliable,Moderate,Unreliable"
    CAPACITY_RATING_FILTER: List[str] = field(
        default_factory=lambda: _get("CAPACITY_RATING_FILTER", "Reliable").split(",")
    )

    # --- Schedules (seconds) ---------------------------------------------------------
    FEE_ACCRUAL_INTERVAL: int = int(_get("FEE_ACCRUAL_INTERVAL", "3600"))
    FUNDING_REPORT_INTERVAL: int = int(_get("FUNDING_REPORT_INTERVAL", "3600"))
    PNL_TRACK_INTERVAL: int = int(_get("PNL_TRACK_INTERVAL", "900"))  # 15 min
    MARKET_REFRESH_INTERVAL: int = int(_get("MARKET_REFRESH_INTERVAL", "21600"))  # 6 h

    # --- Misc ------------------------------------------------------------------------
    DRY_RUN: bool = _get("DRY_RUN", "false").lower() in ("1", "true", "yes")
    LOG_FILE: str = _get("LOG_FILE", "bot/operator.log")
    HEDGE_DRIFT_WARN_PCT: float = float(_get("HEDGE_DRIFT_WARN_PCT", "0.02"))

    # --- Simulated funding accrual (demo) --------------------------------------------
    # When true, funding is computed as perp_notional * live_funding_rate * elapsed
    # instead of read from real on-chain funding payments. Defaults to DRY_RUN.
    SIM_FUNDING: bool = _get("SIM_FUNDING", _get("DRY_RUN", "false")).lower() in ("1", "true", "yes")
    # Accelerate simulated time so yield is visible quickly (1.0 = real time).
    FUNDING_TIME_MULTIPLIER: float = float(_get("FUNDING_TIME_MULTIPLIER", "1.0"))
    # Auto-discover available HIP-3 perp dexes from the data API (handles mainnet names).
    AUTO_DISCOVER_DEXES: bool = _get("AUTO_DISCOVER_DEXES", "true").lower() in ("1", "true", "yes")


CONFIG = Config()
