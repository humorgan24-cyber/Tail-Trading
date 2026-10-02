"""The copy-trading loop: watch personas, translate their trades, execute, account."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from .alerts import Alerts
from .db import DB
from .executor import Account
from .matcher import Matcher
from .polymarket import Polymarket, Trade
from .sizing import round_count, size_buy, size_sell

log = logging.getLogger("tailtrader.engine")


def aggregate(trades: list[Trade]) -> list[Trade]:
    """Merge the many partial fills of one order (same token + side) into one signal,
    oldest first, at the volume-weighted price."""
    out: dict[tuple, Trade] = {}
    for t in sorted(trades, key=lambda t: t.ts):
        k = (t.token_id, t.side)
        if k in out:
            a = out[k]
            shares, usdc = a.shares + t.shares, a.usdc + t.usdc
            out[k] = replace(a, shares=shares, usdc=usdc, price=usdc / shares, ts=max(a.ts, t.ts))
        else:
            out[k] = t
    return list(out.values())


class Engine:
    def __init__(self, settings, db: DB, poly: Polymarket, executor, matcher: Matcher, alerts: Alerts):
        self.s, self.db, self.poly, self.ex, self.matcher, self.alerts = settings, db, poly, executor, matcher, alerts
        self.risk = settings.risk
        self.lock = asyncio.Lock()
        self._acct: Account | None = None
        self._acct_ts = 0.0
        self._bankroll: dict[str, tuple[float, float]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.status: dict[str, dict] = {}

    # ------------------------------------------------------------ lifecycle
    def seed_personas(self) -> None:
        for p in self.s.personas:
            self.db.upsert_persona(p["name"], p["wallet"], p.get("weight", 1.0), p.get("multiplier", 1.0),
                                   p.get("bankroll_usd"), p.get("enabled", True))

    async def run(self) -> None:
        self.seed_personas()
        await self.alerts.send(f"Tail Trader started in {self.s.mode.upper()} mode.")
        await asyncio.gather(self.supervisor(), self.housekeeping(), self.retry_loop())

    async def supervisor(self) -> None:
        while True:
            enabled = {p["name"] for p in self.db.personas(enabled_only=True)}
            for name in enabled - set(self.tasks):
                self.tasks[name] = asyncio.create_task(self.persona_loop(name))
            for name in set(self.tasks) - enabled:
                self.tasks.pop(name).cancel()
                self.status.pop(name, None)
            await asyncio.sleep(5)

    async def persona_loop(self, name: str) -> None:
        backoff = self.s.poll_seconds
        while True:
            p = self.db.one("select * from personas where name=?", (name,))
            if not p:
                return
            try:
                if not p["bootstrapped"]:
                    await self.bootstrap(p)
                await self.poll(p)
                self.status[name] = {"ok": True, "last_poll": int(time.time()), "error": ""}
                backoff = self.s.poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("persona %s poll failed", name)
                self.status[name] = {"ok": False, "last_poll": int(time.time()), "error": str(e)[:200]}
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(backoff)

    async def bootstrap(self, p: dict) -> None:
        """Start following from *now*: mark history as seen and snapshot current holdings
        (so later partial exits can be sized against what they really hold)."""
        trades = await self.poly.recent_trades(p["wallet"], pages=3)
        for t in trades:
            self.db.x("insert or ignore into seen(persona,trade_key,ts) values(?,?,?)", (p["name"], t.key, t.ts))
        for tok, shares in (await self.poly.positions(p["wallet"])).items():
            self.db.set_trader_shares(p["name"], tok, shares)
        self.db.x("update personas set bootstrapped=1 where name=?", (p["name"],))
        await self.alerts.send(f"Now following {p['name']} ({p['wallet'][:10]}…). Copying new trades only.")

    async def poll(self, p: dict) -> None:
        trades = await self.poly.recent_trades(p["wallet"])
        new = [t for t in trades if not self._seen(p["name"], t.key)]
        if trades and len(new) == len(trades):  # burst: look further back
            more = await self.poly.recent_trades(p["wallet"], pages=5)
            new = [t for t in more if not self._seen(p["name"], t.key)]
        if not new:
            return
        # Mark first: never execute the same signal twice, even across a crash.
        for t in new:
            self.db.x("insert or ignore into seen(persona,trade_key,ts) values(?,?,?)", (p["name"], t.key, t.ts))
        for t in aggregate(new):
            held_before = self.db.trader_shares(p["name"], t.token_id)
            try:
                if t.side == "BUY":
                    await self.handle_buy(p, t)
                else:
                    await self.handle_sell(p, t, held_before)
            except Exception as e:
                log.exception("handling %s failed", t.key)
                self._log(p, t, status="error", reason=str(e)[:300])
                await self.alerts.send(f"⚠️ Error copying {p['name']} {t.side} on {t.title}: {e}")
            delta = t.shares if t.side == "BUY" else -t.shares
            self.db.set_trader_shares(p["name"], t.token_id, (held_before or 0) + delta)

    def _seen(self, persona, key) -> bool:
        return self.db.one("select 1 from seen where persona=? and trade_key=?", (persona, key)) is not None

    # ------------------------------------------------------------ helpers
    def _log(self, p, t: Trade, **f) -> int:
        return self.db.log(persona=p["name"], kind=t.side.lower(), title=t.title, outcome=t.outcome,
                           src_side=t.side, src_shares=t.shares, src_price=t.price, src_usdc=t.usdc, **f)

    async def account(self, max_age=30) -> Account:
        if not self._acct or time.time() - self._acct_ts > max_age:
            self._acct, self._acct_ts = await self.ex.account(), time.time()
        return self._acct

    async def trader_bankroll(self, p: dict) -> float:
        if p.get("bankroll_usd"):
            return float(p["bankroll_usd"])
        cached = self._bankroll.get(p["name"])
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        v = await self.poly.portfolio_value(p["wallet"])
        self._bankroll[p["name"]] = (v, time.time())
        return v

    def buys_paused(self) -> str:
        if self.db.get("paused", False):
            return "paused from dashboard"
        if self.db.get("loss_paused", False):
            return "daily loss limit hit"
        return ""

    @staticmethod
    def client_id(persona: str, key: str, n: int = 0) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tailtrader:{persona}:{key}:{n}"))

    # ------------------------------------------------------------ buys
    async def handle_buy(self, p: dict, t: Trade) -> None:
        skip = lambda reason: self._log(p, t, status="skipped", reason=reason)  # noqa: E731
        if why := self.buys_paused():
            return skip(why)
        age = time.time() - t.ts
        if age > self.risk.max_trade_age_seconds:
            return skip(f"signal {int(age)}s old (limit {self.risk.max_trade_age_seconds}s)")
        if not (self.risk.min_price <= t.price <= self.risk.max_price):
            return skip(f"price {t.price:.3f} outside {self.risk.min_price}-{self.risk.max_price} band")

        pm = await self.poly.market(t.condition_id)
        if not pm:
            return skip("could not load Polymarket market details")
        outcome = t.outcome or (pm.outcomes[pm.token_ids.index(t.token_id)] if t.token_id in pm.token_ids else "")
        m = await self.matcher.resolve(pm, outcome)
        if m["status"] != "approved":
            if m["status"] == "review":
                await self._review_alert(m)
                return skip(f"possible Kalshi match {m['ticker']} awaiting your approval ({m['reason']})")
            return skip(f"no equivalent Kalshi market: {m['reason']}")
        ticker, side = m["ticker"], m["side"]

        km = await self.matcher.k.market(ticker)
        if km.get("status") not in ("active", "open"):
            return skip(f"Kalshi market {ticker} is {km.get('status')}")

        acct = await self.account()
        bankroll = await self.trader_bankroll(p)
        limit = min(t.price + self.risk.max_buy_slippage, self.risk.max_price)
        count, usd, notes = size_buy(t.usdc, bankroll, acct.equity, acct.cash, acct.exposure,
                                     p["weight"], p["multiplier"], limit, self.risk)
        if count <= 0:
            return skip("; ".join(notes))

        async with self.lock:
            fill = await self.ex.execute("buy", ticker, side, count, limit, self.client_id(p["name"], t.key))
        self._acct = None
        if fill.filled <= 0:
            self._log(p, t, status="no_fill", ticker=ticker, kalshi_side=side, count=0, price=limit,
                      reason=f"no Kalshi {side.upper()} offers at or below ${limit:.2f}")
            await self.alerts.send(f"⏭️ {p['name']} bought {t.outcome} @{t.price:.2f} on “{t.title}” — "
                                   f"Kalshi {ticker} {side.upper()} too expensive (limit {limit:.2f}).")
            return
        cost = fill.notional + fill.fee
        self.db.add_holding("my_holdings", {"persona": p["name"], "ticker": ticker, "side": side}, fill.filled, cost)
        self._log(p, t, status="filled", ticker=ticker, kalshi_side=side, count=fill.filled,
                  price=fill.avg_price, cost=cost, reason="; ".join(notes))
        await self.alerts.send(
            f"✅ Copied {p['name']}: BUY {fill.filled:g} {side.upper()} {ticker} @ ${fill.avg_price:.2f} "
            f"(${cost:.2f}) — they bought {t.outcome} @{t.price:.2f} (${t.usdc:,.0f}) on “{t.title}”")

    async def _review_alert(self, m: dict) -> None:
        key = f"review_alerted:{m['condition_id']}:{m['outcome']}"
        if not self.db.get(key):
            self.db.set(key, True)
            await self.alerts.send(f"🔎 Possible match needs your approval in the dashboard:\n“{m['poly_title']}” "
                                   f"({m['outcome']}) ↔ Kalshi {m['ticker']} — confidence "
                                   f"{(m['confidence'] or 0):.0%}. {m['reason']}")

    # ------------------------------------------------------------ sells
    async def handle_sell(self, p: dict, t: Trade, held_before: float | None) -> None:
        skip = lambda reason: self._log(p, t, status="skipped", reason=reason)  # noqa: E731
        outcome = t.outcome
        if not outcome:
            pm = await self.poly.market(t.condition_id)
            if pm and t.token_id in pm.token_ids:
                outcome = pm.outcomes[pm.token_ids.index(t.token_id)]
        m = self.matcher.lookup(t.condition_id, outcome)
        if not m or m["status"] != "approved":
            return skip("we never copied this position")
        h = self.db.my_holding(p["name"], m["ticker"], m["side"])
        if not h:
            return skip("we hold no copied position here")
        if not held_before or held_before <= 0:
            after = (await self.poly.positions(p["wallet"], t.condition_id)).get(t.token_id, 0.0)
            held_before = after + t.shares
        count = size_sell(t.shares, held_before, h["count"], self.risk.fractional_contracts)
        if count <= 0:
            return skip("proportional exit rounds to zero contracts")
        limit = max(0.01, t.price - self.risk.max_sell_slippage)
        await self._sell(p, m["ticker"], m["side"], count, limit, self.client_id(p["name"], t.key), t=t)

    async def _sell(self, p, ticker, side, count, limit, cid, t: Trade | None = None, retry_id=None) -> float:
        h = self.db.my_holding(p["name"], ticker, side)
        if not h:
            return 0
        count = min(count, h["count"])
        async with self.lock:
            fill = await self.ex.execute("sell", ticker, side, count, limit, cid, reduce_only=True)
        self._acct = None
        remaining = round(count - fill.filled, 4)
        if fill.filled > 0:
            basis = h["cost"] * fill.filled / h["count"]
            proceeds = fill.notional - fill.fee
            self.db.add_holding("my_holdings", {"persona": p["name"], "ticker": ticker, "side": side},
                                -fill.filled, -basis)
            pnl = proceeds - basis
            fields = dict(status="filled", ticker=ticker, kalshi_side=side, count=fill.filled,
                          price=fill.avg_price, cost=-proceeds, reason=f"realized P&L ${pnl:+.2f}")
            if t:
                self._log(p, t, **fields)
            else:
                self.db.log(persona=p["name"], kind="sell", title=f"retry exit {ticker}", **fields)
            await self.alerts.send(f"✅ Copied {p['name']} exit: SELL {fill.filled:g} {side.upper()} {ticker} "
                                   f"@ ${fill.avg_price:.2f} — P&L ${pnl:+.2f}")
        if remaining > 0 and retry_id is None:
            if t:
                self._log(p, t, status="partial" if fill.filled else "no_fill", ticker=ticker, kalshi_side=side,
                          count=remaining, price=limit, reason="exit pending: retrying")
            self.db.x("insert into sell_retries(persona,ticker,side,count,min_price,expires_ts) values(?,?,?,?,?,?)",
                      (p["name"], ticker, side, remaining, limit,
                       int(time.time()) + self.risk.sell_retry_minutes * 60))
            await self.alerts.send(f"⏳ Exit of {remaining:g} {side.upper()} {ticker} not filled at ≥ ${limit:.2f}; "
                                   f"retrying for {self.risk.sell_retry_minutes} min.")
        return fill.filled

    async def retry_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            for r in self.db.q("select * from sell_retries"):
                p = {"name": r["persona"]}
                h = self.db.my_holding(r["persona"], r["ticker"], r["side"])
                if not h:
                    self.db.x("delete from sell_retries where id=?", (r["id"],))
                    continue
                if time.time() > r["expires_ts"]:
                    self.db.x("delete from sell_retries where id=?", (r["id"],))
                    await self.alerts.send(f"🚨 Could not exit {r['count']:g} {r['side'].upper()} {r['ticker']} "
                                           f"at ≥ ${r['min_price']:.2f}. Position is still open — review it.")
                    continue
                try:
                    filled = await self._sell(p, r["ticker"], r["side"], r["count"], r["min_price"],
                                              self.client_id(r["persona"], f"retry{r['id']}", int(time.time())),
                                              retry_id=r["id"])
                except Exception as e:
                    log.warning("retry sell failed: %s", e)
                    continue
                left = r["count"] - filled
                if left <= 1e-6:
                    self.db.x("delete from sell_retries where id=?", (r["id"],))
                else:
                    self.db.x("update sell_retries set count=? where id=?", (left, r["id"]))

    # ------------------------------------------------------------ housekeeping
    async def housekeeping(self) -> None:
        last_settle = last_snap = 0.0
        while True:
            try:
                try:
                    await self.matcher.refresh_index()
                except Exception as e:
                    log.warning("Kalshi index refresh failed: %s", e)
                acct = await self.account(max_age=0)
                now = time.time()
                if now - last_snap > 300:
                    self.db.x("insert or replace into equity(ts,equity,cash) values(?,?,?)",
                              (int(now), acct.equity, acct.cash))
                    last_snap = now
                await self.daily_guard(acct)
                if now - last_settle > 300:
                    await self.settle()
                    last_settle = now
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping failed")
            await asyncio.sleep(60)

    async def daily_guard(self, acct: Account) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.db.get("day") != today:
            self.db.set("day", today)
            self.db.set("day_start_equity", acct.equity)
            if self.db.get("loss_paused"):
                self.db.set("loss_paused", False)
                await self.alerts.send("New day: daily-loss pause lifted.")
            return
        start = float(self.db.get("day_start_equity") or acct.equity)
        if not self.db.get("loss_paused") and acct.equity < start * (1 - self.risk.daily_loss_limit_pct):
            self.db.set("loss_paused", True)
            await self.alerts.send(f"🛑 Equity fell {1 - acct.equity / start:.1%} today "
                                   f"(${start:,.2f} → ${acct.equity:,.2f}). New buys paused until tomorrow; "
                                   "exits still mirror.")

    async def settle(self) -> None:
        for s in await self.ex.settle():
            for h in self.db.q("select * from my_holdings where ticker=? and side=?", (s["ticker"], s["side"])):
                payout = h["count"] if s["result"] == h["side"] else 0.0
                pnl = payout - h["cost"]
                self.db.log(persona=h["persona"], kind="settle", status="settled", ticker=h["ticker"],
                            kalshi_side=h["side"], count=h["count"], price=1.0 if payout else 0.0,
                            cost=-payout, reason=f"resolved {s['result'].upper()}; P&L ${pnl:+.2f}")
                await self.alerts.send(f"🏁 {h['ticker']} resolved {s['result'].upper()}: "
                                       f"{h['count']:g} {h['side'].upper()} → ${payout:.2f} (P&L ${pnl:+.2f})")
            self.db.x("delete from my_holdings where ticker=? and side=?", (s["ticker"], s["side"]))
        self._acct = None


__all__ = ["Engine", "aggregate", "round_count"]
