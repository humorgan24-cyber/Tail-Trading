"""Phone alerts via Telegram and/or a Discord webhook. Failures never stop trading."""
from __future__ import annotations

import logging

from .http import Http

log = logging.getLogger("tailtrader.alerts")


class Alerts:
    def __init__(self, http: Http, settings):
        self.http = http
        self.tg_token, self.tg_chat = settings.telegram_bot_token, settings.telegram_chat_id
        self.discord = settings.discord_webhook_url
        self.prefix = "🧪 PAPER " if settings.mode == "paper" else "💵 LIVE "

    @property
    def enabled(self) -> bool:
        return bool((self.tg_token and self.tg_chat) or self.discord)

    async def send(self, text: str) -> None:
        text = self.prefix + text
        log.info("ALERT %s", text)
        try:
            if self.tg_token and self.tg_chat:
                await self.http.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                     json={"chat_id": self.tg_chat, "text": text,
                                           "disable_web_page_preview": True}, retries=1)
            if self.discord:
                await self.http.post(self.discord, json={"content": text[:1900]}, retries=1)
        except Exception as e:
            log.warning("alert delivery failed: %s", e)
