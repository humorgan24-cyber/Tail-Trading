"""Position sizing. Pure functions so they can be tested in isolation.

Buys: copy the *fraction of bankroll* the trader committed, applied to the share of
your equity allocated to that persona. Because equity is re-read before every trade,
wins and losses compound automatically.

Sells: copy the *fraction of the position* the trader exited.
"""
from __future__ import annotations

import math


def round_count(c: float, fractional: bool, how: str = "floor") -> float:
    step = 100 if fractional else 1
    f = {"floor": math.floor, "nearest": round}[how]
    return f(round(c * step, 6)) / step


def max_fee_per_contract(price: float) -> float:
    return 0.07 * price * (1 - price) + 0.01


def size_buy(src_usdc: float, trader_bankroll: float, equity: float, cash: float, exposure: float,
             weight: float, multiplier: float, limit: float, risk) -> tuple[float, float, list[str]]:
    """Returns (contracts, dollars, notes). contracts == 0 means skip (reason in notes)."""
    notes: list[str] = []
    if trader_bankroll <= 0:
        return 0, 0, ["trader bankroll unknown"]
    frac = src_usdc / trader_bankroll
    if frac > risk.max_trade_pct_of_allocation:
        notes.append(f"trader used {frac:.1%} of bankroll; capped at {risk.max_trade_pct_of_allocation:.0%}")
        frac = risk.max_trade_pct_of_allocation
    usd = frac * equity * weight * multiplier
    room = risk.max_total_exposure_pct * equity - exposure
    if usd > room:
        notes.append(f"trimmed to exposure limit (${max(room, 0):.2f} room)")
        usd = room
    per = limit + max_fee_per_contract(limit)
    if usd > cash - 0.01:
        notes.append(f"trimmed to available cash ${cash:.2f}")
        usd = cash - 0.01
    if usd < risk.min_order_usd:
        return 0, max(usd, 0), notes + [f"copy size ${max(usd, 0):.2f} below ${risk.min_order_usd:.2f} minimum"]
    count = round_count(usd / per, risk.fractional_contracts)
    if count <= 0:
        return 0, usd, notes + [f"${usd:.2f} buys less than one contract at ${limit:.2f}"]
    return count, count * limit, notes


def size_sell(sold_shares: float, trader_held_before: float, my_count: float, fractional: bool) -> float:
    if my_count <= 0:
        return 0
    frac = 1.0 if trader_held_before <= 0 else min(1.0, sold_shares / trader_held_before)
    if frac >= 0.98:            # full exit (allow for rounding dust on their side)
        return my_count
    c = round_count(my_count * frac, fractional, "nearest")
    return min(c, my_count)
