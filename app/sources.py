"""Outbound HTTP. Every request sends the Freq user agent and fails soft."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .config import USER_AGENT

log = logging.getLogger("freq.sources")

_client: httpx.AsyncClient | None = None
_adsb_lock = asyncio.Lock()
_adsb_next = 0.0


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def get_json(
    url: str,
    *,
    params: dict | None = None,
    timeout: float = 3.0,
) -> Any | None:
    try:
        response = await client().get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        host = url.split("?", 1)[0]
        log.warning("GET failed %s (%s)", host, exc.__class__.__name__)
        return None


async def post_json(url: str, payload: dict, *, timeout: float = 3.0) -> tuple[int, str] | None:
    try:
        response = await client().post(url, json=payload, timeout=timeout)
        return response.status_code, response.text
    except Exception as exc:
        log.warning("POST failed %s (%s)", url.split("?", 1)[0], exc.__class__.__name__)
        return None


async def adsb_slot() -> None:
    """Keep adsb.lol / adsb.fi at or under one request per second total."""
    global _adsb_next
    async with _adsb_lock:
        now = time.monotonic()
        wait = _adsb_next - now
        if wait > 0:
            await asyncio.sleep(wait)
        _adsb_next = time.monotonic() + 1.0


def reset_adsb_slot() -> None:
    global _adsb_next
    _adsb_next = 0.0
