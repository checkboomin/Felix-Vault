"""Market selection + capital allocation.

Two responsibilities:

  1. get_reliable_markets(): fetch all HIP-3 markets across the tracked dexes, compute
     funding APY + a reliability rating from 30d funding history, and filter to the
     "Reliable" markets that clear the minimum viable APY. This mirrors the capacity
     dashboard's consistency methodology (trimmed stats, CV, % time positive, % time
     above the 9% threshold) at the level of detail the bot needs.

  2. compute_allocation(): greedy highest-yield-first allocation of TVL across the
     selected markets, respecting per-market capacity, a per-market TVL cap, and an OI
     participation cap.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    from bot.config import CONFIG
except ImportError:  # when bot/ is on sys.path directly
    from config import CONFIG

LOG = logging.getLogger("felix.operator")

HOURS_PER_YEAR = 24 * 365
THIRTY_DAYS_MS = 30 * 24 * 3600 * 1000


@dataclass
class Market:
    coin: str
    dex: str
    oi_usd: float
    funding_hourly: float          # decimal fraction per hour
    funding_apy: float             # annualised %, = hourly * 24 * 365 * 100
    mark_px: float
    estimated_long_pct: float
    capacity: float                # USDC capacity to 50/50 breakeven, capped at OI
    capacity_rating: str           # Reliable / Moderate / Unreliable
    cv: float = 0.0
    pct_time_positive: float = 0.0
    pct_time_above_9: float = 0.0
    mean_apy: float = 0.0

    @property
    def id(self) -> str:
        """Stable market key matching the vault's keccak(dex:coin) layout in spirit."""
        return f"{self.dex}:{self.coin}"


def annualise(hourly_rate: float) -> float:
    return hourly_rate * HOURS_PER_YEAR * 100.0


def estimate_long_pct(hourly_funding: float, sensitivity: float = 0.0003) -> float:
    """long_pct ≈ 0.5 + funding/sensitivity, capped to [0.5, 0.95]."""
    lp = 0.5 + (hourly_funding / sensitivity)
    return max(0.5, min(0.95, lp))


def _trim(values: List[float], pct: float = 0.05) -> List[float]:
    """Drop the top/bottom `pct` of observations."""
    if not values:
        return []
    s = sorted(values)
    k = int(len(s) * pct)
    if k == 0:
        return s
    return s[k:-k] if len(s) > 2 * k else s


def compute_consistency(funding_hist: List[dict]) -> dict:
    """Trimmed reliability metrics from 30d hourly funding history.

    Returns mean_apy, cv, pct_time_positive, pct_time_above_9 and a rating.
    """
    rates = []
    for ev in funding_hist:
        try:
            rates.append(float(ev["fundingRate"]))
        except (KeyError, TypeError, ValueError):
            continue

    if not rates:
        return {
            "mean_apy": 0.0,
            "cv": 999.0,
            "pct_time_positive": 0.0,
            "pct_time_above_9": 0.0,
            "rating": "Unreliable",
            "n": 0,
        }

    apys = [annualise(r) for r in rates]
    trimmed = _trim(apys)
    if not trimmed:
        trimmed = apys

    mean_apy = statistics.fmean(trimmed)
    std = statistics.pstdev(trimmed) if len(trimmed) > 1 else 0.0
    cv = abs(std / mean_apy) if mean_apy != 0 else 999.0
    pct_positive = 100.0 * sum(1 for a in apys if a > 0) / len(apys)
    pct_above_9 = 100.0 * sum(1 for a in apys if a > CONFIG.MIN_VIABLE_APY) / len(apys)

    # "Reliable" gate per the prototype spec: CV < 0.75, %positive > 80, %>9% > 60.
    if cv < CONFIG.MAX_CV and pct_positive > CONFIG.MIN_PCT_TIME_POSITIVE and pct_above_9 > CONFIG.MIN_PCT_TIME_ABOVE_9:
        rating = "Reliable"
    elif cv < 1.5 and pct_positive > 60 and pct_above_9 > 40:
        rating = "Moderate"
    else:
        rating = "Unreliable"

    return {
        "mean_apy": mean_apy,
        "cv": cv,
        "pct_time_positive": pct_positive,
        "pct_time_above_9": pct_above_9,
        "rating": rating,
        "n": len(apys),
    }


def compute_capacity(oi_usd: float, long_pct: float) -> float:
    """Capacity to 50/50 breakeven, hard-capped at total OI."""
    short_pct = 1.0 - long_pct
    current_short = oi_usd * short_pct
    breakeven_short = oi_usd * 0.50
    cap = max(0.0, breakeven_short - current_short)
    return min(cap, oi_usd)


def _scan_universe(client, dex: str, out: List[Market], now_ms: int) -> None:
    """Scan one dex (empty string = main HL markets) and append results to out."""
    try:
        meta, ctxs = client.meta_and_asset_ctxs(dex)
    except Exception as exc:
        LOG.debug("meta_and_asset_ctxs failed for dex=%r: %s", dex or "main", exc)
        return

    # metaAndAssetCtxs returns [meta, ctxs] where meta is a dict with a
    # "universe" list (each entry a dict with "name"). Older/main responses
    # may already be the list itself.
    if isinstance(meta, dict):
        universe = meta.get("universe", [])
    else:
        universe = meta

    found = 0
    for u, ctx in zip(universe, ctxs):
        coin = u if isinstance(u, str) else u.get("name")
        if not coin:
            continue
        try:
            ctx_d = ctx if isinstance(ctx, dict) else {}
            funding_hourly = float(ctx_d.get("funding", 0.0))
            oi = float(ctx_d.get("openInterest", 0.0))
            mark = float(ctx_d.get("markPx") or ctx_d.get("oraclePx") or 0.0)
        except (TypeError, ValueError):
            continue

        oi_usd = oi * mark
        apy = annualise(funding_hourly)
        long_pct = estimate_long_pct(funding_hourly)

        hist = client.funding_history(coin, now_ms - THIRTY_DAYS_MS)
        cons = compute_consistency(hist)

        label = dex or "main"
        client.remember_dex(coin, dex)
        out.append(
            Market(
                coin=coin,
                dex=label,
                oi_usd=oi_usd,
                funding_hourly=funding_hourly,
                funding_apy=apy,
                mark_px=mark,
                estimated_long_pct=long_pct,
                capacity=compute_capacity(oi_usd, long_pct),
                capacity_rating=cons["rating"],
                cv=cons["cv"],
                pct_time_positive=cons["pct_time_positive"],
                pct_time_above_9=cons["pct_time_above_9"],
                mean_apy=cons["mean_apy"],
            )
        )
        found += 1

    LOG.info("  dex=%-8s  %d coins found", dex or "main", found)


async def get_reliable_markets(client, dexes: Optional[List[str]] = None) -> List[Market]:
    """Fetch HIP-3 markets, score them, return viable ones."""
    dexes = dexes or CONFIG.HIP3_DEXES
    out: List[Market] = []
    now_ms = int(time.time() * 1000)

    for dex in dexes:
        _scan_universe(client, dex, out, now_ms)

    LOG.info("Total coins scanned: %d", len(out))

    # Filter: rating in allowed set AND APY >= threshold (>= so 0.0 passes when threshold=0).
    reliable = [
        m
        for m in out
        if m.funding_apy >= CONFIG.MIN_VIABLE_APY and m.capacity_rating in CONFIG.CAPACITY_RATING_FILTER
    ]
    reliable.sort(key=lambda m: m.funding_apy, reverse=True)
    return reliable


async def compute_allocation(total_usdc: float, markets: List[Market]) -> Dict[str, float]:
    """Greedy highest-yield-first allocation.

    For each market (sorted by APY desc):
        max_deployable = min(capacity, total_usdc * MAX_TVL_PCT_PER_MARKET, oi_usd * MAX_POSITION_SIZE_PCT)
        allocate = min(remaining, max_deployable)
        keep if allocate > MIN_ALLOCATION_USDC
    """
    ranked = sorted(markets, key=lambda m: m.funding_apy, reverse=True)
    remaining = float(total_usdc)
    allocations: Dict[str, float] = {}

    for m in ranked:
        if remaining <= CONFIG.MIN_ALLOCATION_USDC:
            break
        max_deployable = min(
            m.capacity,
            total_usdc * CONFIG.MAX_TVL_PCT_PER_MARKET,
            m.oi_usd * CONFIG.MAX_POSITION_SIZE_PCT,
        )
        allocate = min(remaining, max_deployable)
        if allocate > CONFIG.MIN_ALLOCATION_USDC:
            allocations[m.id] = allocate
            remaining -= allocate

    return allocations
