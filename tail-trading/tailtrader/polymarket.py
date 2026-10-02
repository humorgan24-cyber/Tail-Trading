"""Polymarket public data (no account or keys needed)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .http import Http

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"


def to_seconds(v) -> int:
    """Data API timestamps arrive as epoch s, epoch ms, or ISO-8601 depending on route."""
    if v is None:
        return 0
    if isinstance(v, str):
        s = v.strip()
        if s.replace(".", "", 1).isdigit():
            v = float(s)
        else:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    v = float(v)
    return int(v / 1000) if v > 1e11 else int(v)


def _f(row: dict, *names, default=0.0) -> float:
    for n in names:
        if row.get(n) not in (None, ""):
            return float(row[n])
    return default


def _s(row: dict, *names, default="") -> str:
    for n in names:
        if row.get(n) not in (None, ""):
            return str(row[n])
    return default


@dataclass
class Trade:
    key: str
    ts: int
    condition_id: str
    token_id: str
    side: str           # BUY | SELL
    shares: float
    price: float
    usdc: float
    outcome: str
    title: str
    slug: str
    event_slug: str

    @classmethod
    def from_row(cls, r: dict) -> "Trade | None":
        if _s(r, "type", default="TRADE").upper() != "TRADE" or r.get("is_combo") or r.get("isCombo"):
            return None
        token = _s(r, "token_id", "asset_id", "assetId", "asset")
        side = _s(r, "side").upper()
        shares = _f(r, "size", "shares")
        price = _f(r, "price")
        if not token or side not in ("BUY", "SELL") or shares <= 0:
            return None
        usdc = _f(r, "usdc_size", "usdcSize", "amount", default=shares * price)
        tx = _s(r, "transaction_hash", "transactionHash")
        return cls(
            key=f"{tx}:{token}:{side}:{shares:.6f}:{price:.6f}",
            ts=to_seconds(r.get("timestamp")),
            condition_id=_s(r, "condition_id", "conditionId"),
            token_id=token, side=side, shares=shares, price=price, usdc=usdc,
            outcome=_s(r, "outcome"), title=_s(r, "title"),
            slug=_s(r, "slug"), event_slug=_s(r, "event_slug", "eventSlug"),
        )


@dataclass
class PolyMarket:
    condition_id: str
    question: str
    description: str
    end_date: str
    outcomes: list
    token_ids: list
    closed: bool


class Polymarket:
    def __init__(self, http: Http):
        self.http = http
        self._markets: dict[str, PolyMarket] = {}

    async def recent_trades(self, wallet: str, pages: int = 1, limit: int = 100) -> list[Trade]:
        """Newest-first TRADE activity for a wallet."""
        out, cursor = [], None
        for _ in range(pages):
            params = {"user": wallet, "type": "TRADE", "limit": limit}
            if cursor:
                params["cursor"] = cursor
            body = await self.http.get(f"{DATA}/v2/activity", params=params)
            rows = body.get("data", body) if isinstance(body, dict) else body
            for r in rows or []:
                t = Trade.from_row(r)
                if t:
                    out.append(t)
            cursor = (body.get("pagination") or {}).get("next_cursor") if isinstance(body, dict) else None
            if not cursor:
                break
        return out

    async def portfolio_value(self, wallet: str) -> float:
        body = await self.http.get(f"{DATA}/v2/value", params={"user": wallet})
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, list):
            data = data[0] if data else {}
        return float((data or {}).get("value") or 0)

    async def positions(self, wallet: str, condition_id: str | None = None) -> dict[str, float]:
        """token_id -> current shares for the wallet's open positions."""
        out, cursor = {}, None
        for _ in range(50):
            params = {"user": wallet, "limit": 500}
            if condition_id:
                params["condition"] = condition_id
            if cursor:
                params["cursor"] = cursor
            body = await self.http.get(f"{DATA}/v2/positions", params=params)
            for r in body.get("data") or []:
                tok = _s(r, "token_id", "asset_id", "assetId", "asset")
                if tok:
                    out[tok] = _f(r, "current_size", "currentSize", "size")
            cursor = (body.get("pagination") or {}).get("next_cursor")
            if not cursor:
                break
        return out

    async def market(self, condition_id: str) -> PolyMarket | None:
        if condition_id in self._markets:
            return self._markets[condition_id]
        rows = await self.http.get(f"{GAMMA}/markets", params={"condition_ids": condition_id})
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("markets") or []
        if not rows:
            return None
        m = rows[0]

        def arr(v):
            return json.loads(v) if isinstance(v, str) else (v or [])

        pm = PolyMarket(
            condition_id=condition_id,
            question=m.get("question") or m.get("title") or "",
            description=m.get("description") or "",
            end_date=m.get("endDate") or m.get("end_date") or "",
            outcomes=arr(m.get("outcomes")),
            token_ids=arr(m.get("clobTokenIds")),
            closed=bool(m.get("closed")),
        )
        self._markets[condition_id] = pm
        return pm


def now() -> int:
    return int(datetime.now(timezone.utc).timestamp())
