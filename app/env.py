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
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    sarvam_api_key: SecretStr = SecretStr("")
    attendee_api_key: SecretStr = SecretStr("")
    meeting_public_backend_url: str = ""
    meeting_operator_user_ids: list[UUID] = []
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    meeting_bot_email: str = ""
    meeting_token_key: SecretStr = SecretStr("")
    ice_servers: list[dict] = []
    exotel_account_sid: str = Field(default="", pattern=r"^[A-Za-z0-9_-]*$")
    exotel_api_key: SecretStr = SecretStr("")
    exotel_api_token: SecretStr = SecretStr("")
    exotel_api_host: str = "api.in.exotel.com"
    exotel_caller_id: str = ""
    outreach_public_backend_url: str = Field(default="", max_length=200)
    clinic_public_backend_url: str = Field(default="", max_length=200)
    clinic_operator_user_ids: list[UUID] = []

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

    @field_validator("exotel_api_host")
    @classmethod
    def valid_exotel_host(cls, value):
        if value not in {"api.in.exotel.com", "api.exotel.com"}:
            raise ValueError("Unsupported Exotel region host")
        return value

    @field_validator("meeting_public_backend_url", "outreach_public_backend_url", "clinic_public_backend_url")
    @classmethod
    def valid_service_url(cls, value, info):
        if not value:
            return value
        url = urlsplit(value)
        schemes = {"https"}
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
