"""Kalshi Trade API v2: signed requests, market data, orders, and order-book math."""
from __future__ import annotations

import base64
import math
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .http import Http

PROD = "https://external-api.kalshi.com/trade-api/v2"
DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
API_ROOT = "/trade-api/v2"


# ---------------------------------------------------------------- signing
class Signer:
    def __init__(self, key_id: str, pem: str):
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(pem.encode(), password=None)

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path.split("?")[0]).encode()
        if isinstance(self.key, Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


# ---------------------------------------------------------------- book math
def cents_floor(p: float) -> float:
    return math.floor(round(p * 100, 6)) / 100


def cents_ceil(p: float) -> float:
    return math.ceil(round(p * 100, 6)) / 100


def taker_fee(count: float, price: float) -> float:
    """Kalshi's general taker fee: 0.07 * C * P * (1-P), rounded up to the cent."""
    return math.ceil(round(0.07 * count * price * (1 - price) * 100, 6)) / 100


@dataclass
class Book:
    yes: list  # YES bids [(price, count)], best (highest) first
    no: list   # NO bids  [(price, count)], best first

    @classmethod
    def from_api(cls, body: dict) -> "Book":
        ob = body.get("orderbook_fp") or body.get("orderbook") or {}

        def lv(key):
            rows = ob.get(key) or []
            return sorted(((float(p), float(c)) for p, c in rows), key=lambda x: -x[0])

        return cls(yes=lv("yes_dollars"), no=lv("no_dollars"))

    def levels(self, action: str, side: str) -> list:
        """Executable levels in the price of `side`'s contract, best first."""
        if action == "buy":   # you lift the opposite side's bids
            opp = self.no if side == "yes" else self.yes
            return [(round(1 - p, 4), c) for p, c in opp]          # ascending cost
        return list(self.yes if side == "yes" else self.no)        # descending proceeds

    def best(self, action: str, side: str) -> float | None:
        lv = self.levels(action, side)
        return lv[0][0] if lv else None

    def simulate(self, action: str, side: str, count: float, limit: float):
        """IOC fill against the book. Returns (filled_count, avg_price)."""
        filled = notional = 0.0
        for price, size in self.levels(action, side):
            if (action == "buy" and price > limit + 1e-9) or (action == "sell" and price < limit - 1e-9):
                break
            take = min(size, count - filled)
            filled += take
            notional += take * price
            if filled >= count - 1e-9:
                break
        return filled, (notional / filled if filled else 0.0)


def to_v2_order(action: str, side: str, limit: float) -> tuple[str, float]:
    """Translate (buy|sell, yes|no, limit in that side's price) to the V2 single-book
    (bid|ask on YES, YES price). Limits are rounded to whole cents in the safe direction."""
    lim = cents_floor(limit) if action == "buy" else cents_ceil(limit)
    lim = min(max(lim, 0.01), 0.99)
    if side == "yes":
        return ("bid" if action == "buy" else "ask"), lim
    # Buying NO at <= x  == selling YES at >= 1-x;  selling NO at >= x == buying YES at <= 1-x
    return ("ask" if action == "buy" else "bid"), round(1 - lim, 2)


# ---------------------------------------------------------------- client
class Kalshi:
    def __init__(self, http: Http, base: str = PROD, signer: Signer | None = None):
        self.http, self.base, self.signer = http, base, signer

    async def _get(self, path, params=None, auth=False):
        hf = (lambda: self.signer.headers("GET", API_ROOT + path)) if auth else None
        return await self.http.get(self.base + path, params=params, headers_fn=hf)

    async def _post(self, path, body):
        return await self.http.post(self.base + path, json=body,
                                    headers_fn=lambda: self.signer.headers("POST", API_ROOT + path))

    # public market data
    async def orderbook(self, ticker: str) -> Book:
        return Book.from_api(await self._get(f"/markets/{ticker}/orderbook"))

    async def market(self, ticker: str) -> dict:
        body = await self._get(f"/markets/{ticker}")
        return body.get("market", body)

    async def open_events(self, max_pages: int = 200) -> list[dict]:
        events, cursor = [], None
        for _ in range(max_pages):
            params = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            body = await self._get("/events", params=params)
            events.extend(body.get("events") or [])
            cursor = body.get("cursor")
            if not cursor:
                break
        return events

    # account
    async def balance(self) -> tuple[float, float]:
        """(cash, marked value of open positions) in dollars."""
        b = await self._get("/portfolio/balance", auth=True)
        cash = float(b["balance_dollars"]) if b.get("balance_dollars") else b.get("balance", 0) / 100
        return cash, (b.get("portfolio_value") or 0) / 100

    async def positions(self) -> dict[str, float]:
        """ticker -> signed contracts (+YES / -NO)."""
        out, cursor = {}, None
        for _ in range(50):
            params = {"count_filter": "position", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = await self._get("/portfolio/positions", params=params, auth=True)
            for p in body.get("market_positions") or []:
                out[p["ticker"]] = float(p.get("position_fp") or p.get("position") or 0)
            cursor = body.get("cursor")
            if not cursor:
                break
        return out

    async def create_order(self, ticker: str, book_side: str, count: float, price: float,
                           client_order_id: str, reduce_only: bool = False) -> dict:
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": f"{count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": reduce_only,
        }
        return await self._post("/portfolio/events/orders", body)
