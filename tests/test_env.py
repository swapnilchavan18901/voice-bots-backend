import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.env import Settings, get_settings


@pytest.mark.parametrize(
    "values",
    [
        {"jwt_secret": " " * 40},
        {"database_url": "postgresql://localhost/db"},
        {"meeting_public_backend_url": "http://example.com"},
        {"meeting_public_backend_url": "https://user:password@example.com"},
        {"meeting_token_key": "invalid"},
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
        monkeypatch.setenv("MEETING_BOT_EMAIL", "verified@example.com")
        first = get_settings()
        monkeypatch.setenv("MEETING_BOT_EMAIL", "changed@example.com")
        assert get_settings() is first
        assert get_settings().meeting_bot_email == "verified@example.com"
    finally:
        get_settings.cache_clear()


def test_fixed_policy_is_not_loaded_from_environment(monkeypatch):
    from app.constants import constants

    monkeypatch.setenv("FILLER_DELAY_MS", "9999")
    monkeypatch.setenv("SARVAM_LLM_MODEL", "untrusted-override")
    settings = Settings(_env_file=None)
    assert "filler_delay_ms" not in Settings.model_fields
    assert "sarvam_llm_model" not in Settings.model_fields
    assert constants.FILLER_DELAY_MS == 700
    assert constants.SARVAM_LLM_MODEL == "sarvam-105b"
    assert not hasattr(settings, "filler_delay_ms")
