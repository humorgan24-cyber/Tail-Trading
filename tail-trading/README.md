# Tail Trader

Watches one or more Polymarket traders and mirrors their trades on Kalshi, sized to your account, around the clock.

```
Polymarket trader ──(public activity feed, every 2s)──► Tail Trader ──(Kalshi API)──► your Kalshi account
                                                          │
                       Claude checks the two markets ─────┤  strict: same payout in every scenario, or no trade
                                                          │
                          dashboard + phone alerts ◄──────┘
```

## How it copies

| Trader does on Polymarket | Tail Trader does on Kalshi |
|---|---|
| Buys an outcome with X% of their bankroll | Buys the equivalent Kalshi contract with X% of your equity × persona weight |
| Sells Y% of their position | Sells Y% of your copied position |
| Holds to resolution | Holds to resolution (Kalshi pays out; equity — and future trade sizes — grow) |

**Compounding** is automatic: every copy is sized from your *current* equity, so profits are reinvested and losses shrink future bets.

**Multiple personas**: each gets a `weight` (its share of your equity). Keep the total ≤ 1.0. Positions are tracked per persona, so one trader's exit never sells another's position.

## Risk guards (no added risk beyond the trader's own)

- **Strict matching** – trades only when Claude judges the Kalshi contract pays out in exactly the same states of the world (≥ 90% confidence). 60–90% → shown in the dashboard for one-tap approval. Otherwise skipped and logged. Made-up tickers are rejected.
- **Price guard** – never pays more than the trader's price + 3¢; exits accept down to −6¢, then retry for 30 minutes and alert you if still open.
- **Stale guard** – buys noticed more than 10 minutes late are skipped.
- **Size caps** – one copy ≤ 10% of the persona's allocation; total exposure ≤ 90% of equity.
- **Daily loss limit** – 15% drawdown pauses new buys until midnight UTC.
- **Pause button** – stops new buys instantly; exits keep mirroring so you're never left holding something the trader dumped.
- **No double trades** – each signal is recorded before it's executed; live orders carry a unique client id.

All thresholds live in `config.yaml`.

## Setup (≈20 minutes)

### 1. Collect keys

| What | Where | Needed for |
|---|---|---|
| Anthropic API key | console.anthropic.com → API keys | Automatic strict matching (without it every match waits for your approval) |
| Telegram bot | Message **@BotFather** → `/newbot` → copy token. Message your new bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` | Phone alerts (or use a Discord webhook) |
| Kalshi API key | kalshi.com/account/profile → API Keys → Create (Ed25519). Save the private key file | **Live mode only** — paper mode needs no Kalshi key |

### 2. Deploy to Railway (~$5/month, always on)

1. Put this folder in a **private** GitHub repo.
2. railway.com → New Project → Deploy from GitHub repo → pick it. The included `Dockerfile` and `railway.toml` are used automatically.
3. Service → **Volumes** → add a volume mounted at `/data` (keeps your history and positions across restarts).
4. Service → **Variables** → add the values from `.env.example` (`DASHBOARD_PASSWORD`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; leave `TAILTRADER_MODE=paper`).
5. Settings → Networking → **Generate Domain**. Open it, log in with any username + your password.

Any Docker host works the same way (Fly.io, a DigitalOcean droplet: `docker build -t tail . && docker run -d --restart always -p 8080:8080 -v tail:/data --env-file .env tail`).

### 3. Add a trader

Open their Polymarket profile; the URL contains their `0x…` wallet. Paste it (or the whole URL) into **Follow** on the dashboard with a weight. Only trades made *after* you add them are copied.

### 4. Paper trade for 1–2 weeks

Paper mode fills against Kalshi's **real order book** with real fees, so results are a fair preview. Watch for:
- What share of the trader's trades find a Kalshi match (sports and politics usually do; niche markets often don't).
- `no_fill` rows — Kalshi priced worse than the trader's fill.
- Your P&L vs. theirs.

### 5. Go live

1. Fund Kalshi; create the API key.
2. Add `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY` (paste the PEM; `\n` for newlines is fine) and set `TAILTRADER_MODE=live`. Railway redeploys.
3. Start with small weights. The dashboard badge turns red **LIVE**.

To rehearse with fake money on Kalshi's own demo exchange first, use a demo key and `KALSHI_ENV=demo` (demo markets differ from production, so expect fewer matches).

## Things to know

- **Not every trade can be copied.** Kalshi lists different markets; unmatched trades are skipped, so your results will differ from the trader's.
- **Latency.** Copies land a few seconds after the trader's fill; fast-moving markets may have moved (the price guard skips those).
- **Trader bankroll.** Polymarket reports the value of a trader's open positions, which may exclude idle cash and make their bets look bigger. The 10% cap limits the effect; if you know their real bankroll, set `bankroll_usd` for the persona.
- **Polymarket API version.** Built on Data API v2 (v1 retires Oct 24, 2026) and Kalshi's Create Order V2 endpoint.
- This is software, not financial advice. Prediction-market trading can lose money, and past performance of a copied trader doesn't predict future results.

## Run locally

```
pip install -r requirements.txt
cp .env.example .env   # fill in, then export the variables
python -m tailtrader   # dashboard on http://localhost:8080
python -m unittest -v  # 24 offline tests
```

## Code map

| File | Role |
|---|---|
| `tailtrader/engine.py` | Polling, signal aggregation, buy/sell/retry/settlement loops, daily guard |
| `tailtrader/matcher.py` | Kalshi market index + Claude equivalence judge + mapping cache |
| `tailtrader/sizing.py` | Proportional, compounding position sizing |
| `tailtrader/executor.py` | Paper simulator (real book, real fees) and live executor |
| `tailtrader/kalshi.py` / `polymarket.py` | API clients, request signing, order-book math |
| `tailtrader/web.py` + `static/index.html` | Dashboard and control API |
