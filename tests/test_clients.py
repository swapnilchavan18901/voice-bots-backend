import httpx

from app.ai.meeting.speech import MeetingSpeech
from app.clients import AttendeeClient, create_google_client, create_sarvam_client
from app.env import Settings


async def test_provider_clients_are_isolated_and_close():
    settings = Settings(_env_file=None, sarvam_api_key="test-key", attendee_api_key="attendee")
    async with create_sarvam_client(settings) as sarvam, create_google_client() as google:
        assert sarvam.headers["api-subscription-key"] == "test-key"
        assert "api-subscription-key" not in google.headers
        assert str(sarvam.base_url) == "https://api.sarvam.ai"
    assert sarvam.is_closed and google.is_closed
    attendee = AttendeeClient(settings)
    await attendee.close()
    assert attendee.http.is_closed


async def test_meeting_speech_uses_configured_provider_and_closes(monkeypatch):
    settings = Settings(_env_file=None, sarvam_api_key="test-key")
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"transcript": "  Hello Sia  "})

    http = httpx.AsyncClient(
        base_url="https://api.sarvam.ai",
        transport=httpx.MockTransport(respond),
        headers={"api-subscription-key": "test-key"},
    )
    monkeypatch.setattr("app.ai.meeting.speech.create_sarvam_client", lambda _: http)
    speech = MeetingSpeech(settings)
    try:
        assert await speech.transcribe(b"\x00\x00" * 160) == "Hello Sia"
        assert requests[0].url.path == "/speech-to-text"
        assert requests[0].headers["api-subscription-key"] == "test-key"
    finally:
        await speech.close()
    assert http.is_closed
