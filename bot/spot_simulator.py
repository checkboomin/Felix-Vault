"""Spot leg tracking.

CRITICAL DESIGN NOTE
--------------------
The carry trade requires *long spot + short perp* to be delta-neutral. In this
prototype the spot leg is SIMULATED in memory — no real spot order is ever placed.
The simulated spot is priced off Hyperliquid's own mark price, the exact same
reference the real short perp uses, so the two legs cancel and the only residual
PnL is funding income from the short perp.

This module is deliberately a self-contained, swappable component. The
`SpotTracker` ABC defines the interface; `SimulatedSpotTracker` is the prototype
implementation. In production you drop in a `RealSpotTracker` (Ondo tokenised
equities / HIP-3 spot tokens) with the *same* interface and nothing else in the
bot has to change.

    class SpotTracker:
        open(coin, size, price)               -> position_id
        close(position_id, proportion, price)  -> realised_pnl
        get_pnl(position_id, price)            -> float
        get_all_positions()                    -> dict
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class SpotPosition:
    """One long-spot leg. Mirrors the per-market structure in the spec."""

    position_id: str
    coin: str
    entry_price: float
    size: float                 # units (kept equal to the perp size for neutrality)
    usdc_allocated: float       # USDC notionally committed to the spot leg
    opened_at: float = field(default_factory=time.time)
    current_price: float = 0.0
    unrealised_pnl: float = 0.0

    def __post_init__(self) -> None:
        if self.current_price == 0.0:
            self.current_price = self.entry_price

    def mark(self, price: float) -> float:
        """Update the mark and return the unrealised PnL: (price - entry) * size."""
        self.current_price = price
        self.unrealised_pnl = (price - self.entry_price) * self.size
        return self.unrealised_pnl

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "coin": self.coin,
            "entry_price": self.entry_price,
            "size": self.size,
            "usdc_allocated": self.usdc_allocated,
            "opened_at": self.opened_at,
            "current_price": self.current_price,
            "unrealised_pnl": self.unrealised_pnl,
        }


class SpotTracker(ABC):
    """Swappable interface for the spot leg. See module docstring."""

    @abstractmethod
    def open(self, coin: str, size: float, price: float, usdc_allocated: float) -> str:
        """Open a long spot leg. Returns a position id."""

    @abstractmethod
    def close(self, position_id: str, proportion: float, current_price: float) -> float:
        """Close `proportion` (0..1) of a position. Returns realised PnL on the closed slice."""

    @abstractmethod
    def get_pnl(self, position_id: str, current_price: float) -> float:
        """Mark-to-market unrealised PnL for a position at `current_price`."""

    @abstractmethod
    def get_all_positions(self) -> Dict[str, dict]:
        """Snapshot of all open positions keyed by position id."""

    @abstractmethod
    def get(self, position_id: str) -> Optional[SpotPosition]:
        """Return the raw position object (or None)."""


class SimulatedSpotTracker(SpotTracker):
    """In-memory spot tracker. NO orders are ever placed — pure bookkeeping.

    `position_id` is the vault marketId (or any stable per-market key) so the rest
    of the bot can address spot legs by market.
    """

    def __init__(self) -> None:
        self._positions: Dict[str, SpotPosition] = {}

    # --- SpotTracker interface ----------------------------------------------------

    def open(self, coin: str, size: float, price: float, usdc_allocated: float) -> str:
        position_id = coin  # callers pass a stable key via `coin`; see open_delta_neutral
        self._positions[position_id] = SpotPosition(
            position_id=position_id,
            coin=coin,
            entry_price=price,
            size=size,
            usdc_allocated=usdc_allocated,
        )
        return position_id

    def open_with_id(
        self, position_id: str, coin: str, size: float, price: float, usdc_allocated: float
    ) -> str:
        """Open using an explicit position id (e.g. the vault marketId)."""
        self._positions[position_id] = SpotPosition(
            position_id=position_id,
            coin=coin,
            entry_price=price,
            size=size,
            usdc_allocated=usdc_allocated,
        )
        return position_id

    def close(self, position_id: str, proportion: float, current_price: float) -> float:
        pos = self._positions.get(position_id)
        if pos is None:
            return 0.0
        proportion = max(0.0, min(1.0, proportion))
        closing_size = pos.size * proportion
        realised_pnl = (current_price - pos.entry_price) * closing_size

        pos.size -= closing_size
        pos.usdc_allocated *= (1.0 - proportion)
        pos.mark(current_price)

        if pos.size <= 1e-12:
            del self._positions[position_id]
        return realised_pnl

    def get_pnl(self, position_id: str, current_price: float) -> float:
        pos = self._positions.get(position_id)
        if pos is None:
            return 0.0
        return pos.mark(current_price)

    def get_all_positions(self) -> Dict[str, dict]:
        return {pid: p.to_dict() for pid, p in self._positions.items()}

    def get(self, position_id: str) -> Optional[SpotPosition]:
        return self._positions.get(position_id)

    # --- Convenience --------------------------------------------------------------

    def total_unrealised_pnl(self) -> float:
        return sum(p.unrealised_pnl for p in self._positions.values())

    def __contains__(self, position_id: str) -> bool:
        return position_id in self._positions

    def __len__(self) -> int:
        return len(self._positions)


# --------------------------------------------------------------------------------------
# Production stub — same interface, real spot execution. Left unimplemented on purpose so
# it is obvious where production spot wiring (Ondo / HIP-3 spot) would slot in.
# --------------------------------------------------------------------------------------
class RealSpotTracker(SpotTracker):  # pragma: no cover - production placeholder
    """Drop-in replacement that would place REAL spot orders.

    Implementing these four methods against Ondo tokenised equities or HIP-3 spot
    tokens is the only change required to take the prototype to production.
    """

    def open(self, coin: str, size: float, price: float, usdc_allocated: float) -> str:
        raise NotImplementedError("RealSpotTracker requires production spot venue wiring")

    def close(self, position_id: str, proportion: float, current_price: float) -> float:
        raise NotImplementedError("RealSpotTracker requires production spot venue wiring")

    def get_pnl(self, position_id: str, current_price: float) -> float:
        raise NotImplementedError("RealSpotTracker requires production spot venue wiring")

    def get_all_positions(self) -> Dict[str, dict]:
        raise NotImplementedError("RealSpotTracker requires production spot venue wiring")

    def get(self, position_id: str):
        raise NotImplementedError("RealSpotTracker requires production spot venue wiring")
