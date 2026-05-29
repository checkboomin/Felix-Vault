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
    CAPACITY_RATING_FILTER: List[str] = field(default_factory=lambda: ["Reliable"])
    MAX_POSITION_SIZE_PCT: float = float(_get("MAX_POSITION_SIZE_PCT", "0.45"))
    # Cap any single market at this fraction of total vault TVL.
    MAX_TVL_PCT_PER_MARKET: float = float(_get("MAX_TVL_PCT_PER_MARKET", "0.30"))
    REBALANCE_THRESHOLD: float = float(_get("REBALANCE_THRESHOLD", "0.05"))
    MIN_ALLOCATION_USDC: float = float(_get("MIN_ALLOCATION_USDC", "1000"))

    # --- Reliability gate (matches dashboard "Reliable" definition) ------------------
    MAX_CV: float = float(_get("MAX_CV", "0.75"))
    MIN_PCT_TIME_POSITIVE: float = float(_get("MIN_PCT_TIME_POSITIVE", "80.0"))
    MIN_PCT_TIME_ABOVE_9: float = float(_get("MIN_PCT_TIME_ABOVE_9", "60.0"))

    # --- Schedules (seconds) ---------------------------------------------------------
    FEE_ACCRUAL_INTERVAL: int = int(_get("FEE_ACCRUAL_INTERVAL", "3600"))
    FUNDING_REPORT_INTERVAL: int = int(_get("FUNDING_REPORT_INTERVAL", "3600"))
    PNL_TRACK_INTERVAL: int = int(_get("PNL_TRACK_INTERVAL", "900"))  # 15 min
    MARKET_REFRESH_INTERVAL: int = int(_get("MARKET_REFRESH_INTERVAL", "21600"))  # 6 h

    # --- Misc ------------------------------------------------------------------------
    DRY_RUN: bool = _get("DRY_RUN", "false").lower() in ("1", "true", "yes")
    LOG_FILE: str = _get("LOG_FILE", "bot/operator.log")
    HEDGE_DRIFT_WARN_PCT: float = float(_get("HEDGE_DRIFT_WARN_PCT", "0.02"))


CONFIG = Config()
