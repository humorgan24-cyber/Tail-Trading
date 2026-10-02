"""Dashboard + control API (Starlette)."""
from __future__ import annotations

import base64
import re
import secrets
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

INDEX = (Path(__file__).parent / "static" / "index.html").read_text()
WALLET = re.compile(r"0x[a-fA-F0-9]{40}")


class BasicAuth(BaseHTTPMiddleware):
    def __init__(self, app, password: str):
        super().__init__(app)
        self.password = password

    async def dispatch(self, request, call_next):
        if not self.password or request.url.path == "/healthz":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                if secrets.compare_digest(pw, self.password):
                    return await call_next(request)
            except Exception:
                pass
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="tailtrader"'})


def build_app(engine) -> Starlette:
    db = engine.db

    async def index(_):
        return HTMLResponse(INDEX)

    async def healthz(_):
        return JSONResponse({"ok": True})

    async def state(_):
        try:
            a = await engine.account()
            acct = {"equity": a.equity, "cash": a.cash, "exposure": a.exposure}
        except Exception as e:
            acct = {"error": str(e)}
        start = db.get("paper_start") if engine.s.mode == "paper" else None
        personas = []
        for p in db.personas():
            st = db.one("""select sum(status='filled' and kind='buy') copied, sum(status!='filled') skipped,
                           max(ts) last from log where persona=?""", (p["name"],)) or {}
            hold = db.one("select coalesce(sum(cost),0) c from my_holdings where persona=?", (p["name"],))
            personas.append({**p, **st, "open_cost": hold["c"], "status": engine.status.get(p["name"], {})})
        holdings = db.q("""select h.*, (select kalshi_title from mappings m where m.ticker=h.ticker limit 1) title
                           from my_holdings h order by cost desc""")
        return JSONResponse({
            "mode": engine.s.mode, "now": int(time.time()),
            "paused": bool(db.get("paused", False)), "loss_paused": bool(db.get("loss_paused", False)),
            "day_start_equity": db.get("day_start_equity"), "paper_start": start,
            "account": acct, "personas": personas, "holdings": holdings,
            "log": db.q("select * from log order by id desc limit 150"),
            "reviews": db.q("select * from mappings where status='review' order by updated_ts desc"),
            "mappings": db.q("select * from mappings where status='approved' order by updated_ts desc limit 100"),
            "retries": db.q("select * from sell_retries"),
            "equity": db.q("select ts, equity from equity order by ts desc limit 600")[::-1],
            "alerts": engine.alerts.enabled, "judge": bool(engine.matcher.key),
        })

    async def pause(req: Request):
        body = await req.json()
        db.set("paused", bool(body.get("paused")))
        await engine.alerts.send("⏸️ New buys paused from dashboard." if body.get("paused")
                                 else "▶️ Copying resumed from dashboard.")
        return JSONResponse({"ok": True})

    async def persona(req: Request):
        b = await req.json()
        name = (b.get("name") or "").strip()
        m = WALLET.search(b.get("wallet") or "")
        existing = db.one("select * from personas where name=?", (name,))
        if not name or (not m and not existing):
            return JSONResponse({"error": "Give a name and the trader's 0x… wallet address "
                                          "(or a profile link that contains it)."}, 400)
        e = existing or {}
        db.upsert_persona(name, m.group(0) if m else e["wallet"],
                          float(b.get("weight", e.get("weight", 1.0))),
                          float(b.get("multiplier", e.get("multiplier", 1.0))),
                          (float(b["bankroll_usd"]) if b.get("bankroll_usd") not in (None, "") else e.get("bankroll_usd")),
                          bool(b.get("enabled", e.get("enabled", True))), overwrite=True)
        total = sum(p["weight"] for p in db.personas(enabled_only=True))
        warn = f"Enabled weights add up to {total:.2f} (> 1.0): personas could together over-commit." if total > 1.0001 else ""
        return JSONResponse({"ok": True, "warning": warn})

    async def mapping(req: Request):
        b = await req.json()
        row = db.one("select * from mappings where condition_id=? and outcome=?", (b["condition_id"], b["outcome"]))
        if not row:
            return JSONResponse({"error": "unknown mapping"}, 404)
        if b["action"] == "approve":
            side = b.get("side") or row["side"]
            ticker = b.get("ticker") or row["ticker"]
            if side not in ("yes", "no") or not ticker:
                return JSONResponse({"error": "choose YES or NO"}, 400)
            db.x("update mappings set status='approved', source='manual', side=?, ticker=?, updated_ts=? "
                 "where condition_id=? and outcome=?", (side, ticker, int(time.time()), b["condition_id"], b["outcome"]))
        else:
            db.x("update mappings set status='rejected', source='manual', updated_ts=? where condition_id=? and outcome=?",
                 (int(time.time()), b["condition_id"], b["outcome"]))
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[Route("/", index), Route("/healthz", healthz), Route("/api/state", state),
                Route("/api/pause", pause, methods=["POST"]), Route("/api/persona", persona, methods=["POST"]),
                Route("/api/mapping", mapping, methods=["POST"])],
        middleware=[Middleware(BasicAuth, password=engine.s.dashboard_password)],
    )
