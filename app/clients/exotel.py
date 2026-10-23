"""Exotel Connect Voice AI adapter. Mutating requests must never be retried blindly."""

import asyncio
from urllib.parse import quote

import httpx


class ExotelClient:
    def __init__(self, settings):
        self.settings = settings
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self.http = httpx.AsyncClient(
            base_url=f"https://{settings.exotel_api_host}/v1/Accounts/"
            f"{quote(settings.exotel_account_sid, safe='')}/",
            auth=(
                settings.exotel_api_key.get_secret_value(),
                settings.exotel_api_token.get_secret_value(),
            ),
            timeout=20,
            headers={"Accept": "application/json"},
        )

    async def _throttle(self):
        # Leave headroom below Exotel's 200 requests/minute account limit.
        # Deploy one worker; separate processes cannot share this account limiter.
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self._next_request_at - loop.time()))
            self._next_request_at = loop.time() + 0.4

    async def dial(self, number, stream_url, callback_url, call_id):
        await self._throttle()
        response = await self.http.post(
            "Calls/connect",
            data={
                "From": number,
                "CallerId": self.settings.exotel_caller_id,
                "StreamUrl": stream_url,
                "StreamType": "bidirectional",
                "TimeLimit": "1800",
                "Record": "false",
                "CustomField": str(call_id),
                "StatusCallback": callback_url,
                "StatusCallbackEvents[]": ["ringing", "answered", "terminal"],
                "StatusCallbackContentType": "application/json",
            },
        )
        response.raise_for_status()
        return response.json()["Call"]

    async def details(self, sid):
        await self._throttle()
        response = await self.http.get(f"Calls/{quote(sid, safe='')}.json")
        response.raise_for_status()
        return response.json()["Call"]

    async def close(self):
        await self.http.aclose()
