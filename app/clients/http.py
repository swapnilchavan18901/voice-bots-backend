"""HTTP client construction; callers own and close each returned client."""

import httpx


def create_google_client():
    return httpx.AsyncClient(timeout=30, follow_redirects=False)


def create_sarvam_client(settings):
    return httpx.AsyncClient(
        base_url="https://api.sarvam.ai",
        timeout=60,
        headers={"api-subscription-key": settings.sarvam_api_key.get_secret_value()},
    )
