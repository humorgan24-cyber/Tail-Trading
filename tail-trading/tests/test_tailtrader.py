"""Offline tests: run with `python -m unittest -v` from the project folder."""
import asyncio
import base64
import time
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from tailtrader.config import Matching, Risk, Settings
from tailtrader.db import DB
from tailtrader.engine import Engine, aggregate
from tailtrader.executor import PaperExecutor
from tailtrader.kalshi import Book, Signer, taker_fee, to_v2_order
from tailtrader.matcher import KalshiIndex, Matcher, parse_verdict
from tailtrader.polymarket import PolyMarket, Trade, to_seconds
from tailtrader.sizing import size_buy, size_sell


def run(c):
    return asyncio.run(c)


def trade(key, side="BUY", shares=100, price=0.40, ts=None, token="TOK_YES", outcome="Yes", cond="C1"):
    return Trade(key=key, ts=ts or int(time.time()), condition_id=cond, token_id=token, side=side,
                 shares=shares, price=price, usdc=shares * price, outcome=outcome,
                 title="Will the Fed cut rates in December 2026?", slug="", event_slug="")


# --------------------------------------------------------------------------- fakes
class FakePoly:
    def __init__(self):
        self.feed, self.value, self.pos = [], 10_000.0, {}
        self.mk = PolyMarket("C1", "Will the Fed cut rates in December 2026?", "Resolves YES if the FOMC lowers...",
                             "2026-12-10", ["Yes", "No"], ["TOK_YES", "TOK_NO"], False)

    async def recent_trades(self, wallet, pages=1, limit=100):
        return sorted(self.feed, key=lambda t: -t.ts)

    async def portfolio_value(self, wallet):
        return self.value

    async def positions(self, wallet, condition_id=None):
        return dict(self.pos)

    async def market(self, cid):
        return self.mk if cid == "C1" else None


class FakeKalshi:
    def __init__(self):
        # YES bids up to 0.38; NO bids up to 0.59 => YES asks from 0.41
        self.book = Book(yes=[(0.38, 500), (0.37, 500)], no=[(0.59, 300), (0.58, 1000)])
        self.mkt = {"ticker": "KXFED-26DEC-CUT", "status": "active", "result": "",
                    "yes_bid_dollars": "0.3800", "no_bid_dollars": "0.5900"}

    async def orderbook(self, t):
        return self.book

    async def market(self, t):
        return dict(self.mkt)

    async def open_events(self):
        return [{"event_ticker": "KXFED-26DEC", "title": "Fed rate cut in December 2026?",
                 "markets": [{"ticker": "KXFED-26DEC-CUT", "status": "active", "yes_sub_title": "Cut",
                              "close_time": "2026-12-10T19:00:00Z", "rules_primary": "If the Fed cuts..."}]}]


class FakeAlerts:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def make_engine(**risk):
    s = Settings(mode="paper", paper_starting_balance=1000, risk=Risk(**risk), matching=Matching())
    db = DB(":memory:")
    poly, k = FakePoly(), FakeKalshi()
    ex = PaperExecutor(db, k, 1000)
    m = Matcher(db, k, None, s.matching, anthropic_key="")
    e = Engine(s, db, poly, ex, m, FakeAlerts())
    db.upsert_persona("whale", "0x" + "a" * 40, weight=1.0)
    return e, db, poly, k


def approve(db):
    db.x("""insert into mappings(condition_id,outcome,ticker,side,confidence,status,source,reason,poly_title,updated_ts)
            values('C1','Yes','KXFED-26DEC-CUT','yes',0.97,'approved','auto','same','Fed',?)""", (int(time.time()),))
    db.x("""insert into mappings(condition_id,outcome,ticker,side,confidence,status,source,reason,poly_title,updated_ts)
            values('C1','No','KXFED-26DEC-CUT','no',0.97,'approved','auto','same','Fed',?)""", (int(time.time()),))


# --------------------------------------------------------------------------- unit
class TestOrderMath(unittest.TestCase):
    def test_v2_translation(self):
        self.assertEqual(to_v2_order("buy", "yes", 0.437), ("bid", 0.43))   # never pay above limit
        self.assertEqual(to_v2_order("buy", "no", 0.437), ("ask", 0.57))    # NO<=0.43 == sell YES>=0.57
        self.assertEqual(to_v2_order("sell", "yes", 0.431), ("ask", 0.44))  # never sell below limit
        self.assertEqual(to_v2_order("sell", "no", 0.431), ("bid", 0.56))

    def test_book_levels_and_fill(self):
        b = FakeKalshi().book
        self.assertEqual(b.best("buy", "yes"), 0.41)
        self.assertEqual(b.best("buy", "no"), 0.62)
        self.assertEqual(b.simulate("buy", "yes", 400, 0.42), (400, (300 * 0.41 + 100 * 0.42) / 400))
        self.assertEqual(b.simulate("buy", "yes", 400, 0.41)[0], 300)       # limit respected
        self.assertEqual(b.simulate("sell", "yes", 100, 0.38), (100, 0.38))
        self.assertEqual(b.simulate("sell", "yes", 100, 0.39)[0], 0)

    def test_book_from_api(self):
        b = Book.from_api({"orderbook_fp": {"yes_dollars": [["0.10", "5.00"], ["0.42", "13.00"]],
                                            "no_dollars": [["0.56", "17.00"]]}})
        self.assertEqual(b.yes[0], (0.42, 13.0))
        self.assertAlmostEqual(b.best("buy", "yes"), 0.44)

    def test_fee(self):
        self.assertEqual(taker_fee(100, 0.5), 1.75)
        self.assertEqual(taker_fee(1, 0.5), 0.02)


class TestSizing(unittest.TestCase):
    def test_proportional_and_compounding(self):
        r = Risk()
        # trader puts 2% of a $10k bankroll in; we copy 2% of $1,000 = $20
        c1, usd1, _ = size_buy(200, 10_000, 1000, 1000, 0, 1.0, 1.0, 0.43, r)
        c2, usd2, _ = size_buy(200, 10_000, 2000, 2000, 0, 1.0, 1.0, 0.43, r)
        self.assertEqual(c1, 43)  # $20 / (0.43 + fee headroom)
        self.assertGreater(c2, c1 * 1.9)  # doubled equity => ~double size (compounding)

    def test_caps(self):
        r = Risk(max_trade_pct_of_allocation=0.10)
        c, usd, notes = size_buy(5000, 10_000, 1000, 1000, 0, 1.0, 1.0, 0.5, r)
        self.assertLessEqual(usd, 100.0)
        self.assertTrue(any("capped" in n for n in notes))
        c, usd, notes = size_buy(500, 10_000, 1000, 1000, 880, 1.0, 1.0, 0.5, r)  # exposure 88% of 90%
        self.assertLessEqual(usd, 20.0)
        c, _, notes = size_buy(1, 10_000, 1000, 1000, 0, 1.0, 1.0, 0.5, r)
        self.assertEqual(c, 0)

    def test_weights(self):
        r = Risk()
        a, _, _ = size_buy(200, 10_000, 1000, 1000, 0, 0.5, 1.0, 0.4, r)
        b, _, _ = size_buy(200, 10_000, 1000, 1000, 0, 1.0, 1.0, 0.4, r)
        self.assertAlmostEqual(a, b / 2, delta=1)

    def test_sell(self):
        self.assertEqual(size_sell(50, 100, 40, False), 20)
        self.assertEqual(size_sell(99, 100, 40, False), 40)    # ~full exit
        self.assertEqual(size_sell(200, 100, 40, False), 40)
        self.assertEqual(size_sell(10, 0, 40, False), 40)      # unknown holding -> full


class TestParsing(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(to_seconds(1782752879000), 1782752879)
        self.assertEqual(to_seconds("1782752879"), 1782752879)
        self.assertEqual(to_seconds("2026-09-08T17:03:34Z"), 1788887014)

    def test_trade_row_v2(self):
        t = Trade.from_row({"proxy_wallet": "0x1", "timestamp": 1782752879, "condition_id": "0xc", "type": "TRADE",
                            "size": 25, "usdc_size": 12.25, "transaction_hash": "0xh", "price": 0.49,
                            "token_id": "123", "side": "BUY", "title": "T", "outcome": "Yes"})
        self.assertEqual((t.shares, t.usdc, t.side, t.token_id), (25, 12.25, "BUY", "123"))
        self.assertIsNone(Trade.from_row({"type": "REDEEM", "size": 5}))

    def test_aggregate(self):
        a = aggregate([trade("a", shares=10, price=0.40, ts=1), trade("b", shares=30, price=0.44, ts=2),
                       trade("c", side="SELL", shares=5, price=0.5, ts=3)])
        self.assertEqual(len(a), 2)
        self.assertAlmostEqual(a[0].price, (4 + 13.2) / 40)
        self.assertEqual(a[0].shares, 40)

    def test_verdict(self):
        v = parse_verdict('Sure:\n{"ticker": "KX-1", "side": "YES", "confidence": 0.93, "reason": "same"}')
        self.assertEqual((v["ticker"], v["side"]), ("KX-1", "yes"))
        self.assertIsNone(parse_verdict('{"ticker": null, "side": "null", "confidence": 0.1}')["side"])


class TestSigning(unittest.TestCase):
    def check(self, key):
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        h = Signer("kid", pem).headers("GET", "/trade-api/v2/portfolio/balance?limit=5")
        msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/v2/portfolio/balance").encode()
        sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
        pub = key.public_key()
        if isinstance(key, ed25519.Ed25519PrivateKey):
            pub.verify(sig, msg)
        else:
            pub.verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                             salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())

    def test_rsa(self):
        self.check(rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def test_ed25519(self):
        self.check(ed25519.Ed25519PrivateKey.generate())


class TestMatcher(unittest.TestCase):
    def test_candidates_rank(self):
        idx = KalshiIndex()
        idx.build([
            {"event_ticker": "E1", "title": "Bitcoin above $150k by end of 2026?",
             "markets": [{"ticker": "BTC", "status": "active", "yes_sub_title": "Above 150k", "close_time": "2026-12-31T00:00:00Z"}]},
            {"event_ticker": "E2", "title": "Fed rate cut in December 2026?",
             "markets": [{"ticker": "FED", "status": "active", "yes_sub_title": "Cut", "close_time": "2026-12-10T19:00:00Z"}]},
        ])
        c = idx.candidates("Will the Fed cut interest rates in December 2026?", "2026-12-10")
        self.assertEqual(c[0]["market"]["ticker"], "FED")

    def test_resolve_with_judge(self):
        e, db, poly, k = make_engine()
        m = e.matcher
        m.key = "test"

        async def judge(pm, outcome, cands):
            return {"ticker": "KXFED-26DEC-CUT", "side": "yes" if outcome == "Yes" else "no",
                    "confidence": 0.95 if outcome == "Yes" else 0.7, "reason": "x"}
        m.judge = judge
        self.assertEqual(run(m.resolve(poly.mk, "Yes"))["status"], "approved")
        self.assertEqual(run(m.resolve(poly.mk, "No"))["status"], "review")

    def test_resolve_without_key_needs_review(self):
        e, db, poly, k = make_engine()
        row = run(e.matcher.resolve(poly.mk, "Yes"))
        self.assertEqual((row["status"], row["ticker"], row["side"]), ("review", "KXFED-26DEC-CUT", None))

    def test_hallucinated_ticker_rejected(self):
        e, db, poly, k = make_engine()
        e.matcher.key = "test"

        async def judge(*a):
            return {"ticker": "MADE-UP", "side": "yes", "confidence": 0.99, "reason": "x"}
        e.matcher.judge = judge
        self.assertEqual(run(e.matcher.resolve(poly.mk, "Yes"))["status"], "rejected")


# --------------------------------------------------------------------------- end to end
class TestEngine(unittest.TestCase):
    def test_full_cycle(self):
        e, db, poly, k = make_engine()
        approve(db)
        p = db.one("select * from personas")
        poly.feed = [trade("old", ts=int(time.time()) - 5000)]
        poly.pos = {"TOK_YES": 100.0}
        run(e.bootstrap(p))
        p = db.one("select * from personas")
        run(e.poll(p))
        self.assertEqual(db.q("select * from log"), [])            # history not copied
        self.assertEqual(db.trader_shares("whale", "TOK_YES"), 100.0)

        # Trader buys $200 (2% of $10k) of YES @0.40, in two partial fills.
        poly.feed += [trade("b1", shares=300, price=0.40), trade("b2", shares=200, price=0.40)]
        run(e.poll(p))
        buy = db.one("select * from log where kind='buy'")
        self.assertEqual(buy["status"], "filled", buy["reason"])
        self.assertEqual(buy["src_shares"], 500)
        h = db.my_holding("whale", "KXFED-26DEC-CUT", "yes")
        self.assertEqual(h["count"], 43)                            # ~$20 / (0.43 + fee room)
        self.assertLess(float(db.get("paper_cash")), 1000 - 43 * 0.41)
        self.assertEqual(db.trader_shares("whale", "TOK_YES"), 600)

        # Trader exits half of their 600 shares -> we exit half of ours.
        poly.feed.append(trade("s1", side="SELL", shares=300, price=0.39))
        run(e.poll(p))
        self.assertEqual(db.my_holding("whale", "KXFED-26DEC-CUT", "yes")["count"], 21)
        sell = db.one("select * from log where kind='sell' and status='filled'")
        self.assertIn("realized P&L", sell["reason"])

        # Same signals seen again are ignored (idempotent).
        n = len(db.q("select * from log"))
        run(e.poll(p))
        self.assertEqual(len(db.q("select * from log")), n)

        # Market resolves YES -> paper account is paid $1/contract.
        cash = float(db.get("paper_cash"))
        k.mkt.update(status="finalized", result="yes")
        run(e.settle())
        self.assertAlmostEqual(float(db.get("paper_cash")), cash + 21)
        self.assertIsNone(db.my_holding("whale", "KXFED-26DEC-CUT", "yes"))
        self.assertTrue(any("resolved YES" in a for a in e.alerts.sent))

    def test_buy_no_side(self):
        e, db, poly, k = make_engine()
        approve(db)
        p = {**db.one("select * from personas"), "bootstrapped": 1}
        run(e.poll(p))
        poly.feed.append(trade("n1", shares=500, price=0.60, token="TOK_NO", outcome="No"))
        run(e.poll(p))
        log = db.one("select * from log")
        self.assertEqual((log["status"], log["kalshi_side"]), ("filled", "no"))
        self.assertAlmostEqual(log["price"], 0.62)

    def test_guards(self):
        e, db, poly, k = make_engine()
        approve(db)
        p = db.one("select * from personas")
        db.x("update personas set bootstrapped=1")
        poly.feed = [trade("stale", ts=int(time.time()) - 3600)]
        run(e.poll(p))
        self.assertIn("old", db.one("select reason from log order by id desc")["reason"])

        poly.feed.append(trade("pricey", price=0.30))                  # Kalshi asks 0.41 > 0.33
        run(e.poll(p))
        self.assertEqual(db.one("select status from log order by id desc")["status"], "no_fill")

        db.set("paused", True)
        poly.feed.append(trade("paused", price=0.40, token="TOK_NO", outcome="No"))
        run(e.poll(p))
        self.assertIn("paused", db.one("select reason from log order by id desc")["reason"])

    def test_unmatched_market_is_skipped_not_traded(self):
        e, db, poly, k = make_engine()
        p = {**db.one("select * from personas"), "bootstrapped": 1}
        poly.feed.append(trade("x1"))
        run(e.poll(p))
        row = db.one("select * from log")
        self.assertEqual(row["status"], "skipped")
        self.assertIn("awaiting your approval", row["reason"])
        self.assertIsNone(db.my_holding("whale", "KXFED-26DEC-CUT", "yes"))
        self.assertEqual(float(db.get("paper_cash")), 1000)

    def test_sell_retry_when_book_thin(self):
        e, db, poly, k = make_engine()
        approve(db)
        p = {**db.one("select * from personas"), "bootstrapped": 1}
        db.set_trader_shares("whale", "TOK_YES", 0)
        poly.feed.append(trade("b1", shares=500, price=0.40))
        run(e.poll(p))
        k.book = Book(yes=[(0.20, 1000)], no=[(0.59, 300)])            # bids collapse
        poly.feed.append(trade("s1", side="SELL", shares=500, price=0.39))
        run(e.poll(p))
        self.assertEqual(len(db.q("select * from sell_retries")), 1)
        self.assertEqual(db.my_holding("whale", "KXFED-26DEC-CUT", "yes")["count"], 43)

    def test_daily_loss_pause(self):
        e, db, poly, k = make_engine(daily_loss_limit_pct=0.10)
        from tailtrader.executor import Account
        run(e.daily_guard(Account(1000, 1000, 0)))
        run(e.daily_guard(Account(890, 890, 0)))
        self.assertTrue(db.get("loss_paused"))


if __name__ == "__main__":
    unittest.main()
