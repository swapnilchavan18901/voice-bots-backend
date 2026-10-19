"""Single cached source of validated process configuration, including the .env file."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/voice_bots"
    jwt_secret: SecretStr = Field(min_length=32)
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    sarvam_api_key: SecretStr = SecretStr("")
    sarvam_llm_model: str = "sarvam-105b"
    sarvam_stt_model: str = "saaras:v4"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_voice: str = "shubh"
    attendee_api_key: SecretStr = SecretStr("")
    attendee_api_url: str = "https://app.attendee.dev"
    meeting_public_backend_url: str = ""
    meeting_voice_name: str = Field(default="Sia", min_length=1, max_length=60)
    meeting_voice_language: str = "en-IN"
    meeting_filler_delay_ms: int = Field(default=700, ge=0, le=5000)
    meeting_voice_seconds: int = Field(default=14400, ge=60, le=28800)
    meeting_empty_leave_seconds: int = Field(default=30, ge=1, le=600)
    meeting_voice_max_participants: int = Field(default=30, ge=1, le=100)
    meeting_operator_user_ids: list[UUID] = []
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    meeting_bot_email: str = ""
    meeting_token_key: SecretStr = SecretStr("")
    meeting_sync_days: int = Field(default=30, ge=1, le=90)
    ice_servers: list[dict] = []
    max_voice_sessions: int = Field(default=20, ge=1)
    voice_session_seconds: int = Field(default=1800, ge=30)

    @field_validator("database_url")
    @classmethod
    def valid_database_url(cls, value):
        try:
            url = make_url(value)
            if url.drivername not in {"postgresql+asyncpg", "sqlite+aiosqlite"}:
                raise ValueError
        except Exception:
            raise ValueError(
                "DATABASE_URL must use postgresql+asyncpg or sqlite+aiosqlite"
            ) from None
        return value

    @field_validator("attendee_api_url", "meeting_public_backend_url")
    @classmethod
    def valid_service_url(cls, value, info):
        if not value and info.field_name == "meeting_public_backend_url":
            return value
        url = urlsplit(value)
        schemes = (
            {"https"} if info.field_name == "meeting_public_backend_url" else {"http", "https"}
        )
        if (
            url.scheme not in schemes
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Expected an absolute service URL without credentials, query or fragment"
            )
        try:
            url.port
        except ValueError:
            raise ValueError("Invalid service URL port") from None
        return value

    @field_validator(
        "sarvam_llm_model",
        "sarvam_stt_model",
        "sarvam_tts_model",
        "sarvam_voice",
        "meeting_voice_name",
        "meeting_voice_language",
    )
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value.strip()

    @field_validator("jwt_secret")
    @classmethod
    def valid_jwt_secret(cls, value):
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 non-padding characters")
        return value

    @field_validator("meeting_token_key")
    @classmethod
    def valid_encryption_key(cls, value):
        if value.get_secret_value():
            try:
                Fernet(value.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError("MEETING_TOKEN_KEY must be a valid Fernet key") from None
        return value

    @field_validator("cors_origins")
    @classmethod
    def valid_origins(cls, values):
        for value in values:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path
            ):
                raise ValueError("CORS_ORIGINS must contain HTTP(S) origins without paths")
        return values

    @field_validator("ice_servers")
    @classmethod
    def valid_ice_servers(cls, values):
        for server in values:
            if set(server) - {"urls", "username", "credential"}:
                raise ValueError("Unsupported ICE server option")
            urls = server.get("urls")
            urls = [urls] if isinstance(urls, str) else urls
            if (
                not isinstance(urls, list)
                or not urls
                or any(
                    not isinstance(url, str)
                    or not url.startswith(("stun:", "stuns:", "turn:", "turns:"))
                    for url in urls
                )
            ):
                raise ValueError("ICE servers require STUN or TURN URLs")
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
