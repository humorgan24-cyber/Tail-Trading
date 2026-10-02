"""Small HTTP helper: `requests` run off the event loop, with retry/backoff."""
from __future__ import annotations

import asyncio
import logging
import random

import requests

log = logging.getLogger("tailtrader.http")


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status, self.body, self.url = status, body, url


class Http:
    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self.s = session or requests.Session()
        self.s.headers.setdefault("User-Agent", "tailtrader/1.0")
        self.timeout = timeout

    async def request(self, method: str, url: str, *, params=None, json=None, headers=None,
                      retries: int = 3, headers_fn=None):
        """headers_fn() is called per attempt (fresh signatures/timestamps)."""
        delay = 0.5
        for attempt in range(retries + 1):
            h = dict(headers or {})
            if headers_fn:
                h.update(headers_fn())
            try:
                r = await asyncio.to_thread(self.s.request, method, url, params=params, json=json,
                                            headers=h, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == retries:
                    raise
                log.warning("%s %s failed (%s); retrying", method, url, e)
            else:
                if r.status_code < 400:
                    return r.json() if r.content else {}
                retryable = r.status_code == 429 or r.status_code >= 500
                if not retryable or attempt == retries:
                    raise HTTPError(r.status_code, r.text, url)
                log.warning("%s %s -> %s; retrying", method, url, r.status_code)
            await asyncio.sleep(delay + random.random() * 0.25)
            delay = min(delay * 2, 8)

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)
