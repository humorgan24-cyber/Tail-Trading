"""Strict Polymarket -> Kalshi contract matching.

1. A local index of every open Kalshi market narrows thousands of markets to a
   handful of candidates (text similarity + date proximity).
2. Claude reads both sides' resolution rules and picks the Kalshi position that
   pays out in exactly the same states of the world -- or says there is none.
3. Only matches at or above `min_confidence` are traded. Near-misses go to the
   dashboard for one-tap approval; everything else is skipped and logged.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import defaultdict
from datetime import datetime

from .db import DB
from .http import Http
from .kalshi import Kalshi
from .polymarket import PolyMarket

log = logging.getLogger("tailtrader.match")

STOP = set("""a an the of in on at to for by will be is are was were and or vs v versus with
from before after than this that what which who win wins won 2024 2025 2026 2027 2028 market
yes no game match""".split())


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower()) if t not in STOP and len(t) > 1]


def _date(s) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(str(s)[:10]).timestamp()
        except ValueError:
            return None


class KalshiIndex:
    def __init__(self):
        self.entries: list[dict] = []
        self.inv: dict[str, set[int]] = defaultdict(set)
        self.idf: dict[str, float] = {}
        self.by_ticker: dict[str, dict] = {}
        self.built_at = 0.0

    def build(self, events: list[dict]) -> None:
        entries, inv = [], defaultdict(set)
        for ev in events:
            for m in ev.get("markets") or []:
                if m.get("status") not in (None, "active", "open", "initialized"):
                    continue
                text = " ".join(filter(None, [ev.get("title"), ev.get("sub_title"),
                                              m.get("yes_sub_title"), m.get("title")]))
                e = {"event": ev, "market": m, "text": text, "toks": set(tokens(text))}
                i = len(entries)
                entries.append(e)
                for t in e["toks"]:
                    inv[t].add(i)
        n = max(len(entries), 1)
        self.entries, self.inv = entries, inv
        self.idf = {t: math.log(1 + n / len(ix)) for t, ix in inv.items()}
        self.by_ticker = {e["market"]["ticker"]: e for e in entries}
        self.built_at = time.time()
        log.info("Kalshi index: %d open markets in %d events", len(entries), len(events))

    def candidates(self, question: str, end_date: str = "", k_events: int = 6, per_event: int = 25) -> list[dict]:
        q = set(tokens(question))
        if not q:
            return []
        qn = math.sqrt(sum(self.idf.get(t, 0) ** 2 for t in q)) or 1
        scores: dict[int, float] = defaultdict(float)
        for t in q:
            for i in self.inv.get(t, ()):
                scores[i] += self.idf[t] ** 2
        end = _date(end_date)
        ranked = []
        for i, s in scores.items():
            e = self.entries[i]
            en = math.sqrt(sum(self.idf.get(t, 0) ** 2 for t in e["toks"])) or 1
            score = s / (qn * en)
            close = _date(e["market"].get("expected_expiration_time") or e["market"].get("close_time"))
            if end and close:
                days = abs(close - end) / 86400
                score *= 1.15 if days <= 2 else (1.0 if days <= 14 else 0.7)
            ranked.append((score, i))
        ranked.sort(reverse=True)
        events: dict[str, list] = {}
        for score, i in ranked:
            e = self.entries[i]
            et = e["event"]["event_ticker"]
            if et not in events:
                if len(events) >= k_events:
                    continue
                events[et] = []
            if len(events[et]) < per_event:
                events[et].append({**e, "score": round(score, 3)})
        return [m for ms in events.values() for m in ms]


JUDGE_PROMPT = """You are verifying a copy-trade between two prediction markets. Precision matters more than coverage: a wrong match loses real money, a missed match costs nothing.

POLYMARKET MARKET
Question: {question}
Ends: {end}
Resolution rules: {rules}

The trader bought/sold the outcome: "{outcome}" (one share pays $1 if this outcome happens).

KALSHI CANDIDATES (each has a YES side and a NO side; each contract pays $1)
{cands}

Task: find the single Kalshi ticker and side (yes/no) that pays $1 in EXACTLY the same states of the world as one Polymarket "{outcome}" share. Check subject, threshold/line, date and deadline (including time zone), whether ties/draws/postponements are treated the same, and the resolution source. If any plausible real-world scenario resolves them differently, it is NOT a match (you may still name it with confidence below 0.6).

Reply with JSON only:
{{"ticker": "<ticker or null>", "side": "yes|no|null", "confidence": <0.0-1.0>, "reason": "<one sentence; name any difference>"}}"""


def _cand_text(c: dict) -> str:
    m, ev = c["market"], c["event"]
    rules = (m.get("rules_primary") or "")[:500]
    sec = (m.get("rules_secondary") or "")[:200]
    return (f"- ticker={m['ticker']} | event: {ev.get('title','')} | yes_means: {m.get('yes_sub_title','')} "
            f"| closes: {m.get('close_time','')} | rules: {rules} {sec}").strip()


class Matcher:
    def __init__(self, db: DB, kalshi: Kalshi, http: Http, cfg, anthropic_key: str):
        self.db, self.k, self.http, self.cfg, self.key = db, kalshi, http, cfg, anthropic_key
        self.index = KalshiIndex()

    async def refresh_index(self, force=False) -> None:
        if force or time.time() - self.index.built_at > self.cfg.index_refresh_minutes * 60:
            self.index.build(await self.k.open_events())

    def lookup(self, condition_id: str, outcome: str) -> dict | None:
        return self.db.one("select * from mappings where condition_id=? and outcome=?", (condition_id, outcome))

    def save(self, pm: PolyMarket | None, condition_id, outcome, ticker, side, conf, status, source, reason):
        kt = ""
        if ticker and ticker in self.index.by_ticker:
            e = self.index.by_ticker[ticker]
            kt = f"{e['event'].get('title','')} — {e['market'].get('yes_sub_title','')}"
        self.db.x("""insert into mappings(condition_id,outcome,ticker,side,confidence,status,source,reason,
                     poly_title,kalshi_title,updated_ts) values(?,?,?,?,?,?,?,?,?,?,?)
                     on conflict(condition_id,outcome) do update set ticker=excluded.ticker, side=excluded.side,
                     confidence=excluded.confidence, status=excluded.status, source=excluded.source,
                     reason=excluded.reason, poly_title=coalesce(excluded.poly_title, poly_title),
                     kalshi_title=excluded.kalshi_title, updated_ts=excluded.updated_ts""",
                  (condition_id, outcome, ticker, side, conf, status, source, reason,
                   pm.question if pm else None, kt, int(time.time())))
        return self.lookup(condition_id, outcome)

    async def resolve(self, pm: PolyMarket, outcome: str) -> dict:
        """Return the mapping row; status 'approved' means safe to trade."""
        row = self.lookup(pm.condition_id, outcome)
        if row:
            if row["status"] == "approved" or row["source"] == "manual":
                return row
            if row["status"] == "review":
                return row
            if time.time() - row["updated_ts"] < self.cfg.no_match_retry_hours * 3600:
                return row
        await self.refresh_index()
        cands = self.index.candidates(pm.question, pm.end_date, k_events=self.cfg.candidates)
        if not cands:
            return self.save(pm, pm.condition_id, outcome, None, None, 0, "rejected", "auto",
                             "No similar open Kalshi market")
        if not self.key:
            best = cands[0]["market"]
            return self.save(pm, pm.condition_id, outcome, best["ticker"], None, cands[0]["score"], "review",
                             "auto", "No ANTHROPIC_API_KEY: closest text match, side and rules need your review")
        verdict = await self.judge(pm, outcome, cands)
        ticker, side = verdict.get("ticker"), verdict.get("side")
        conf = float(verdict.get("confidence") or 0)
        valid = ticker in {c["market"]["ticker"] for c in cands} and side in ("yes", "no")
        if valid and conf >= self.cfg.min_confidence:
            status = "approved"
        elif valid and conf >= self.cfg.review_confidence:
            status = "review"
        else:
            status = "rejected"
        return self.save(pm, pm.condition_id, outcome, ticker if valid else None, side if valid else None,
                         conf, status, "auto", verdict.get("reason", ""))

    async def judge(self, pm: PolyMarket, outcome: str, cands: list[dict]) -> dict:
        prompt = JUDGE_PROMPT.format(question=pm.question, end=pm.end_date, rules=pm.description[:2500],
                                     outcome=outcome, cands="\n".join(_cand_text(c) for c in cands))
        try:
            body = await self.http.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": self.cfg.model, "max_tokens": 400,
                      "messages": [{"role": "user", "content": prompt}]})
            text = "".join(b.get("text", "") for b in body.get("content", []))
            return parse_verdict(text)
        except Exception as e:
            log.exception("match judge failed")
            return {"ticker": None, "side": None, "confidence": 0, "reason": f"judge error: {e}"}


def parse_verdict(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"ticker": None, "side": None, "confidence": 0, "reason": "unparseable judge reply"}
    v = json.loads(m.group(0))
    for k in ("ticker", "side"):
        if v.get(k) in ("null", "", None):
            v[k] = None
    if v.get("side"):
        v["side"] = str(v["side"]).lower()
    return v
