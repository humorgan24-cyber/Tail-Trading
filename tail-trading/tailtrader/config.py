"""Settings: config.yaml for behaviour, environment variables for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Risk:
    # Never pay more than the trader's fill price + this (dollars per contract).
    max_buy_slippage: float = 0.03
    # Accept down to the trader's exit price - this when copying a sell.
    max_sell_slippage: float = 0.06
    # Ignore buys we notice later than this (stale signal = different trade).
    max_trade_age_seconds: int = 600
    # Hard cap on any single copied buy, as a share of the persona's allocation.
    max_trade_pct_of_allocation: float = 0.10
    # Never have more than this share of equity tied up in open positions.
    max_total_exposure_pct: float = 0.90
    # Pause new buys for the rest of the UTC day after this drawdown.
    daily_loss_limit_pct: float = 0.15
    min_order_usd: float = 1.00
    # Skip buys priced outside this band (lottery tickets / near-certainties).
    min_price: float = 0.02
    max_price: float = 0.98
    # Kalshi supports fractional contracts on some markets; whole is safest.
    fractional_contracts: bool = False
    # Keep retrying an unfinished copied sell for this long.
    sell_retry_minutes: int = 30


@dataclass
class Matching:
    min_confidence: float = 0.90
    review_confidence: float = 0.60
    model: str = "claude-sonnet-5-5"
    candidates: int = 6
    no_match_retry_hours: float = 6.0
    index_refresh_minutes: int = 10


@dataclass
class Settings:
    mode: str = "paper"                 # paper | live
    paper_starting_balance: float = 1000.0
    poll_seconds: float = 2.0
    db_path: str = "data/tailtrader.db"
    personas: list = field(default_factory=list)
    risk: Risk = field(default_factory=Risk)
    matching: Matching = field(default_factory=Matching)

    # Secrets (environment only)
    kalshi_key_id: str = ""
    kalshi_private_key: str = ""        # PEM text
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    dashboard_password: str = ""
    port: int = 8080

    @property
    def live(self) -> bool:
        return self.mode == "live"


def _pem_from_env() -> str:
    pem = os.getenv("KALSHI_PRIVATE_KEY", "")
    path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not pem and path and Path(path).exists():
        pem = Path(path).read_text()
    # Hosting dashboards often flatten newlines into literal "\n".
    return pem.replace("\\n", "\n").strip()


def load(path: str | None = None) -> Settings:
    path = path or os.getenv("TAILTRADER_CONFIG", "config.yaml")
    raw = {}
    if Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}
    s = Settings(
        mode=os.getenv("TAILTRADER_MODE", raw.get("mode", "paper")).lower(),
        paper_starting_balance=float(raw.get("paper_starting_balance", 1000)),
        poll_seconds=float(raw.get("poll_seconds", 2)),
        db_path=os.getenv("TAILTRADER_DB", raw.get("db_path", "data/tailtrader.db")),
        personas=raw.get("personas") or [],
        risk=Risk(**(raw.get("risk") or {})),
        matching=Matching(**(raw.get("matching") or {})),
        kalshi_key_id=os.getenv("KALSHI_KEY_ID", ""),
        kalshi_private_key=_pem_from_env(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
        port=int(os.getenv("PORT", "8080")),
    )
    if s.mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {s.mode!r}")
    if s.live and not (s.kalshi_key_id and s.kalshi_private_key):
        raise ValueError("live mode needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY")
    return s
