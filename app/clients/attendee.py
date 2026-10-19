"""Attendee REST transport. Credentials remain on the backend."""

import httpx
from fastapi import HTTPException


class AttendeeClient:
    def __init__(self, settings):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False)

    async def request(self, method, path, **kwargs):
        key = self.settings.attendee_api_key.get_secret_value()
        if not key:
            raise HTTPException(503, "ATTENDEE_API_KEY is not configured.")
        try:
            response = await self.http.request(
                method,
                f"{self.settings.attendee_api_url.rstrip('/')}/api/v1/{path}",
                headers={"Authorization": f"Token {key}"},
                **kwargs,
            )
        except httpx.HTTPError:
            raise HTTPException(
                502, "Attendee did not respond. Check bot status before retrying."
            ) from None
        if response.status_code >= 400:
            detail = {
                401: "Attendee rejected the API key.",
                403: "Attendee account access is unavailable.",
                429: "Attendee rate limit reached. Try again later.",
            }.get(
                response.status_code,
                f"Attendee rejected the request (HTTP {response.status_code}).",
            )
            raise HTTPException(502, detail)
        return response.json() if response.content else {}

    async def close(self):
        await self.http.aclose()
