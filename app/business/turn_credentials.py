"""Prepare expiring Metered credentials before calls and retain rotation overlap."""

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import time
from collections import deque
from datetime import UTC, datetime

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.constants import constants
from app.db.models import TurnCredentialState

logger = logging.getLogger(__name__)


class MeteredLogFilter(logging.Filter):
    """Metered requires its management secret in the query string."""

    def filter(self, record):
        record.msg = re.sub(
            r"([?&]secretKey=)[^&\s\"']+", r"\1[REDACTED]", record.getMessage()
        )
        record.args = ()
        return True


# httpx logs complete request URLs at INFO, including authentication query parameters.
if not any(isinstance(f, MeteredLogFilter) for f in logging.getLogger("httpx").filters):
    logging.getLogger("httpx").addFilter(MeteredLogFilter())


class TurnCredentials:
    def __init__(self, settings, session_factory, *, http=None, clock=time.time):
        self.settings = settings
        self.session_factory = session_factory
        self.http = http
        self.clock = clock
        self.task = None
        self.lock = asyncio.Lock()
        self.requests = {}
        # Keep the original domain separator so existing encrypted caches survive renames.
        key = hashlib.sha256(
            b"sia-turn-credentials-v1\0"
            + settings.jwt_secret.get_secret_value().encode()
        ).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.scope = hashlib.sha256(
            (
                settings.metered_domain
                + "\0"
                + settings.metered_secret_key.get_secret_value()
            ).encode()
        ).hexdigest()

    @property
    def enabled(self):
        return bool(
            self.settings.metered_domain
            and self.settings.metered_secret_key.get_secret_value()
        )

    def start(self):
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self._rotate())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def _decode(self, row):
        if row and row.encrypted_state:
            try:
                state = json.loads(self.cipher.decrypt(row.encrypted_state.encode()))
                if state.get("scope") == self.scope:
                    return state
            except (InvalidToken, ValueError, TypeError):
                logger.warning(
                    "TURN credential cache could not be decrypted; preparing a replacement"
                )
        return {"scope": self.scope, "credentials": [], "retry_at": 0}

    async def _create(self):
        client = self.http or httpx.AsyncClient(timeout=10)
        try:
            response = await client.post(
                f"https://{self.settings.metered_domain}/api/v1/turn/credential",
                params={
                    "secretKey": self.settings.metered_secret_key.get_secret_value()
                },
                json={
                    "expiryInSeconds": constants.TURN_CREDENTIAL_SECONDS,
                    "label": "audiva-voice",
                },
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError("Invalid TURN credential response")
            if not all(
                isinstance(data.get(k), str) and data[k]
                for k in ("username", "password")
            ):
                raise ValueError("Invalid TURN credential response")
            ttl = data.get("expiryInSeconds")
            if type(ttl) is not int or ttl != constants.TURN_CREDENTIAL_SECONDS:
                raise ValueError("Unexpected TURN credential lifetime")
            return {"username": data["username"], "password": data["password"]}
        finally:
            if self.http is None:
                await client.aclose()

    async def refresh(self):
        if not self.enabled:
            return
        async with self.lock, self.session_factory() as db, db.begin():
            insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
            await db.execute(
                insert(TurnCredentialState)
                .values(id=1, encrypted_state="")
                .on_conflict_do_nothing()
            )
            row = await db.scalar(
                select(TurnCredentialState)
                .where(TurnCredentialState.id == 1)
                .with_for_update()
            )
            state = self._decode(row)
            now = self.clock()
            state["credentials"] = [
                c for c in state["credentials"] if c["expires_at"] > now
            ]
            latest = max(
                state["credentials"], key=lambda c: c["created_at"], default=None
            )
            if (
                not latest
                or now >= latest["created_at"] + constants.TURN_ROTATION_SECONDS
            ) and now >= state["retry_at"]:
                # Reserve the retry interval even on failure; requests never create credentials.
                state["retry_at"] = now + constants.TURN_RETRY_SECONDS
                try:
                    credential = await self._create()
                    credential.update(
                        created_at=now,
                        ready_at=self.clock() + constants.TURN_PROPAGATION_SECONDS,
                        expires_at=now + constants.TURN_CREDENTIAL_SECONDS,
                    )
                    state["credentials"].append(credential)
                except (httpx.HTTPError, ValueError, TypeError):
                    # Provider exceptions can contain the secret URL or response credentials.
                    logger.warning(
                        "TURN credential creation failed; retrying after the cooldown"
                    )
            row.encrypted_state = self.cipher.encrypt(
                json.dumps(state).encode()
            ).decode()

    async def _rotate(self):
        while True:
            try:
                await self.refresh()
            except SQLAlchemyError:
                logger.warning(
                    "TURN credential rotation unavailable; check database and migration status"
                )
            await asyncio.sleep(constants.TURN_REFRESH_SECONDS)

    def throttle(self, user_id):
        now = self.clock()
        self.requests = {
            key: hits
            for key, hits in self.requests.items()
            if hits and hits[-1] > now - 60
        }
        hits = self.requests.setdefault(user_id, deque())
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= constants.TURN_REQUESTS_PER_MINUTE:
            raise HTTPException(
                429,
                "Too many voice configuration requests",
                headers={"Retry-After": "60", "Cache-Control": "no-store"},
            )
        hits.append(now)

    async def configuration(self):
        if not self.enabled:
            if (
                self.settings.metered_domain
                or self.settings.metered_secret_key.get_secret_value()
            ):
                raise HTTPException(
                    503,
                    "Voice relay configuration is incomplete",
                    headers={"Cache-Control": "no-store"},
                )
            return {"ice_servers": [{"urls": constants.STUN_URL}], "expires_at": None}
        async with self.session_factory() as db:
            row = await db.get(TurnCredentialState, 1)
            credentials = self._decode(row)["credentials"]
        now = self.clock()
        ready = [
            c
            for c in credentials
            if c["ready_at"] <= now
            and c["expires_at"] > now + constants.TURN_MIN_REMAINING_SECONDS
        ]
        if not ready:
            pending = [c for c in credentials if c["ready_at"] > now]
            retry = min((math.ceil(c["ready_at"] - now) for c in pending), default=30)
            raise HTTPException(
                503,
                "Voice relay is warming up. Please try again in two minutes.",
                headers={
                    "Retry-After": str(max(1, retry)),
                    "Cache-Control": "no-store",
                },
            )
        credential = max(ready, key=lambda c: c["created_at"])
        return {
            "ice_servers": [{"urls": constants.STUN_URL}]
            + [
                {
                    "urls": url,
                    "username": credential["username"],
                    "credential": credential["password"],
                }
                for url in constants.TURN_URLS
            ],
            "expires_at": datetime.fromtimestamp(credential["expires_at"], UTC),
        }
