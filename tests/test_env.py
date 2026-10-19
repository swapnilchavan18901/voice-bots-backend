import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.env import Settings, get_settings


@pytest.mark.parametrize(
    "values",
    [
        {"jwt_secret": " " * 40},
        {"database_url": "postgresql://localhost/db"},
        {"attendee_api_url": "not-a-url"},
        {"meeting_public_backend_url": "http://example.com"},
        {"meeting_public_backend_url": "https://user:password@example.com"},
        {"meeting_token_key": "invalid"},
        {"sarvam_voice": "  "},
        {"meeting_voice_seconds": 0},
        {"cors_origins": ["https://example.com/path"]},
        {"ice_servers": [{"urls": "https://example.com"}]},
    ],
)
def test_invalid_configuration_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_optional_integrations_can_be_disabled():
    settings = Settings(_env_file=None)
    assert not settings.attendee_api_key.get_secret_value()
    assert not settings.meeting_token_key.get_secret_value()


def test_valid_configuration_and_cached_environment(monkeypatch):
    settings = Settings(
        _env_file=None,
        meeting_token_key=Fernet.generate_key().decode(),
        meeting_public_backend_url="https://example.com",
        ice_servers=[{"urls": ["stun:stun.example.com:3478"]}],
    )
    assert settings.meeting_public_backend_url == "https://example.com"
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("MEETING_VOICE_NAME", "Test name")
        first = get_settings()
        monkeypatch.setenv("MEETING_VOICE_NAME", "Changed")
        assert get_settings() is first
        assert get_settings().meeting_voice_name == "Test name"
    finally:
        get_settings.cache_clear()
