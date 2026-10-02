"""Entrypoint: `python -m tailtrader` runs the copier and the dashboard together."""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from . import config
from .alerts import Alerts
from .db import DB
from .engine import Engine
from .executor import LiveExecutor, PaperExecutor
from .http import Http
from .kalshi import DEMO, PROD, Kalshi, Signer
from .matcher import Matcher
from .polymarket import Polymarket
from .web import build_app


def build(settings) -> Engine:
    http = Http()
    db = DB(settings.db_path)
    base = DEMO if os.getenv("KALSHI_ENV", "prod").lower() == "demo" else PROD
    signer = Signer(settings.kalshi_key_id, settings.kalshi_private_key) if settings.kalshi_private_key else None
    kalshi = Kalshi(http, base, signer)
    executor = LiveExecutor(db, kalshi) if settings.live else PaperExecutor(db, kalshi, settings.paper_starting_balance)
    matcher = Matcher(db, kalshi, http, settings.matching, settings.anthropic_api_key)
    return Engine(settings, db, Polymarket(http), executor, matcher, Alerts(http, settings))


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = config.load()
    engine = build(settings)
    server = uvicorn.Server(uvicorn.Config(build_app(engine), host="0.0.0.0", port=settings.port,
                                           log_level="warning"))
    logging.getLogger("tailtrader").info("Dashboard on port %s, mode=%s", settings.port, settings.mode)
    await asyncio.gather(engine.run(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
