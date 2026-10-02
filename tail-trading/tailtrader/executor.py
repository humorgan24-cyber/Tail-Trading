"""Order execution: a paper simulator against Kalshi's live order book, and live trading."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .db import DB
from .kalshi import Kalshi, taker_fee, to_v2_order

log = logging.getLogger("tailtrader.exec")


@dataclass
class Fill:
    filled: float
    avg_price: float   # in the price of the side traded (YES or NO contract)
    fee: float

    @property
    def notional(self) -> float:
        return self.filled * self.avg_price


@dataclass
class Account:
    equity: float      # cash + marked open positions
    cash: float
    exposure: float    # cost basis of open positions


class PaperExecutor:
    """Fills IOC orders against the real Kalshi book (no keys needed) with real fee math."""
    mode = "paper"

    def __init__(self, db: DB, kalshi: Kalshi, starting_balance: float):
        self.db, self.k = db, kalshi
        if db.get("paper_cash") is None:
            db.set("paper_cash", starting_balance)
            db.set("paper_start", starting_balance)

    async def execute(self, action, ticker, side, count, limit, client_id, reduce_only=False) -> Fill:
        if action == "sell":
            pos = self.db.one("select count from paper_positions where ticker=? and side=?", (ticker, side))
            count = min(count, pos["count"] if pos else 0)
            if count <= 0:
                return Fill(0, 0, 0)
        book = await self.k.orderbook(ticker)
        # Price at the same cent granularity the live order would use.
        _, lim = to_v2_order(action, side, limit)
        lim = lim if side == "yes" else round(1 - lim, 2)
        filled, avg = book.simulate(action, side, count, lim)
        if filled <= 0:
            return Fill(0, 0, 0)
        fee = taker_fee(filled, avg)
        cash = float(self.db.get("paper_cash"))
        if action == "buy":
            cost = filled * avg + fee
            if cost > cash:  # should not happen (engine sizes to cash) but never go negative
                filled = max(0.0, (cash - fee) / avg) // 1
                if filled <= 0:
                    return Fill(0, 0, 0)
                fee = taker_fee(filled, avg)
                cost = filled * avg + fee
            self.db.set("paper_cash", cash - cost)
            self.db.add_holding("paper_positions", {"ticker": ticker, "side": side}, filled, filled * avg)
        else:
            pos = self.db.one("select count, cost from paper_positions where ticker=? and side=?", (ticker, side))
            basis = pos["cost"] * filled / pos["count"]
            self.db.set("paper_cash", cash + filled * avg - fee)
            self.db.add_holding("paper_positions", {"ticker": ticker, "side": side}, -filled, -basis)
        return Fill(filled, avg, fee)

    async def account(self) -> Account:
        cash = float(self.db.get("paper_cash"))
        marked = exposure = 0.0
        for p in self.db.q("select * from paper_positions"):
            exposure += p["cost"]
            try:
                m = await self.k.market(p["ticker"])
                bid = float(m.get(f"{p['side']}_bid_dollars") or 0)
            except Exception:  # keep running on a transient error; mark at cost
                bid = p["cost"] / p["count"]
            marked += p["count"] * bid
        return Account(cash + marked, cash, exposure)

    async def settle(self) -> list[dict]:
        """Pay out paper positions in markets Kalshi has resolved."""
        done = []
        for p in self.db.q("select * from paper_positions"):
            m = await self.k.market(p["ticker"])
            result = (m.get("result") or "").lower()
            if m.get("status") in ("determined", "finalized", "settled") and result in ("yes", "no"):
                payout = p["count"] if result == p["side"] else 0.0
                self.db.set("paper_cash", float(self.db.get("paper_cash")) + payout)
                self.db.x("delete from paper_positions where ticker=? and side=?", (p["ticker"], p["side"]))
                done.append({**p, "result": result, "payout": payout})
        return done


class LiveExecutor:
    mode = "live"

    def __init__(self, db: DB, kalshi: Kalshi):
        self.db, self.k = db, kalshi

    async def execute(self, action, ticker, side, count, limit, client_id, reduce_only=False) -> Fill:
        book_side, price = to_v2_order(action, side, limit)
        r = await self.k.create_order(ticker, book_side, count, price, client_id,
                                      reduce_only=reduce_only or action == "sell")
        filled = float(r.get("fill_count") or 0)
        if filled <= 0:
            return Fill(0, 0, 0)
        avg_yes = float(r.get("average_fill_price") or price)
        avg = avg_yes if side == "yes" else 1 - avg_yes
        fee = float(r.get("average_fee_paid") or 0) * filled
        return Fill(filled, round(avg, 6), round(fee, 4))

    async def account(self) -> Account:
        cash, marked = await self.k.balance()
        exposure = sum(r["cost"] for r in self.db.q("select cost from my_holdings"))
        return Account(cash + marked, cash, exposure)

    async def settle(self) -> list[dict]:
        """Kalshi pays out automatically; we only clear attribution for resolved markets."""
        done = []
        for t in {r["ticker"] for r in self.db.q("select ticker from my_holdings")}:
            m = await self.k.market(t)
            result = (m.get("result") or "").lower()
            if m.get("status") in ("determined", "finalized", "settled") and result in ("yes", "no"):
                for side in {h["side"] for h in self.db.q("select side from my_holdings where ticker=?", (t,))}:
                    done.append({"ticker": t, "side": side, "result": result})
        return done
