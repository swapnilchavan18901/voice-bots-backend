"""Verify TURN access controls, rotation, restart recovery, and secret handling offline."""

import asyncio
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import bots as bots_api
from app.api import public as public_api
from app.api.dependencies import current_user
from app.business.turn_credentials import TurnCredentials
from app.constants import constants
from app.db.models import Base, TurnCredentialState, utcnow
from app.db.session import get_db
from app.env import Settings


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'turn.db'}")
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        jwt_secret="turn-test-jwt-secret-at-least-32-characters",
        metered_domain="test-app.metered.live",
        metered_secret_key="management-secret-never-in-browser",
    )
    now = [1_800_000_000.0]
    calls = []
    state = {"status": 200, "ttl": constants.TURN_CREDENTIAL_SECONDS}

    def provider(request):
        calls.append(request)
        return httpx.Response(
            state["status"],
            json={
                "username": f"temporary-user-{len(calls)}",
                "password": f"temporary-password-{len(calls)}",
                "expiryInSeconds": state["ttl"],
                "apiKey": "credential-api-key-not-for-browser",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:

        def service(config=settings):
            return TurnCredentials(config, sessions, http=http, clock=lambda: now[0])

        yield SimpleNamespace(
            service=service,
            settings=settings,
            sessions=sessions,
            now=now,
            calls=calls,
            state=state,
            manager=service(),
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_propagation_encryption_and_restart_reuse(env):
    await env.manager.refresh()
    with pytest.raises(HTTPException) as exc:
        await env.manager.configuration()
    assert exc.value.status_code == 503
    assert exc.value.headers["Retry-After"] == "120"
    async with env.sessions() as db:
        row = await db.get(TurnCredentialState, 1)
        assert "temporary-password" not in row.encrypted_state
        assert "management-secret" not in row.encrypted_state
    env.now[0] += constants.TURN_PROPAGATION_SECONDS
    config = await env.manager.configuration()
    assert len(config["ice_servers"]) == 5
    assert config["ice_servers"][1]["credential"] == "temporary-password-1"
    assert [s["urls"] for s in config["ice_servers"][1:]] == list(constants.TURN_URLS)
    assert "management-secret" not in str(config)
    assert "credential-api-key" not in str(config)
    restarted = env.service()
    await restarted.refresh()
    assert await restarted.configuration() == config
    assert len(env.calls) == 1
    request = env.calls[0]
    assert request.url.host == "test-app.metered.live"
    assert (
        json.loads(request.content)["expiryInSeconds"]
        == constants.TURN_CREDENTIAL_SECONDS
    )


@pytest.mark.asyncio
async def test_rotation_preserves_old_calls_and_waits_for_new_credential(env):
    await env.manager.refresh()
    env.now[0] += constants.TURN_ROTATION_SECONDS
    await env.manager.refresh()
    assert len(env.calls) == 2
    assert (await env.manager.configuration())["ice_servers"][1][
        "username"
    ] == "temporary-user-1"
    env.now[0] += constants.TURN_PROPAGATION_SECONDS
    assert (await env.manager.configuration())["ice_servers"][1][
        "username"
    ] == "temporary-user-2"
    async with env.sessions() as db:
        state = env.manager._decode(await db.get(TurnCredentialState, 1))
    assert len(state["credentials"]) == 2
    assert all(request.method == "POST" for request in env.calls)


@pytest.mark.asyncio
async def test_refuses_credentials_that_might_expire_mid_call(env):
    await env.manager.refresh()
    env.now[0] += (
        constants.TURN_CREDENTIAL_SECONDS - constants.TURN_MIN_REMAINING_SECONDS
    )
    with pytest.raises(HTTPException) as exc:
        await env.manager.configuration()
    assert exc.value.status_code == 503
    assert len(env.calls) == 1  # Request handlers never mint credentials.


@pytest.mark.asyncio
async def test_concurrent_rotation_and_failure_cooldown_persist(env):
    env.state["status"] = 403
    await asyncio.gather(*(env.manager.refresh() for _ in range(5)))
    assert len(env.calls) == 1
    await env.service().refresh()
    assert len(env.calls) == 1
    env.now[0] += constants.TURN_RETRY_SECONDS
    env.state["status"] = 200
    await env.manager.refresh()
    assert len(env.calls) == 2


@pytest.mark.asyncio
async def test_provider_logs_do_not_leak_management_secret(env, caplog):
    env.state["status"] = 401
    with caplog.at_level(logging.INFO):
        await env.manager.refresh()
    assert "management-secret-never-in-browser" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "temporary-password" not in caplog.text


@pytest.mark.asyncio
async def test_provider_must_confirm_the_requested_expiration(env):
    env.state["ttl"] = None
    await env.manager.refresh()
    env.now[0] += constants.TURN_PROPAGATION_SECONDS
    with pytest.raises(HTTPException):
        await env.manager.configuration()


@pytest.mark.asyncio
async def test_changed_provider_credentials_cannot_reuse_old_cache(env):
    await env.manager.refresh()
    env.now[0] += constants.TURN_PROPAGATION_SECONDS
    settings = env.settings.model_copy(
        update={"metered_domain": "other-app.metered.live"}
    )
    with pytest.raises(HTTPException):
        await env.service(settings).configuration()


@pytest.mark.asyncio
async def test_stun_only_local_mode_and_incomplete_config(env):
    settings = Settings(_env_file=None, jwt_secret="x" * 32)
    config = await env.service(settings).configuration()
    assert config == {"ice_servers": [{"urls": constants.STUN_URL}], "expires_at": None}
    settings = settings.model_copy(update={"metered_domain": "test.metered.live"})
    with pytest.raises(HTTPException) as exc:
        await env.service(settings).configuration()
    assert exc.value.status_code == 503


def test_credential_endpoint_rate_limit(env):
    for _ in range(constants.TURN_REQUESTS_PER_MINUTE):
        env.manager.throttle("user")
    with pytest.raises(HTTPException) as exc:
        env.manager.throttle("user")
    assert exc.value.status_code == 429
    env.manager.throttle("another-user")
    env.now[0] += 61
    env.manager.throttle("user")
    assert len(env.manager.requests["user"]) == 1


@pytest.mark.asyncio
async def test_dashboard_requires_auth_membership_and_disables_caching(
    env, monkeypatch
):
    app = FastAPI()
    app.include_router(bots_api.router, prefix="/api/v1")
    app.state.turn = env.manager
    app.dependency_overrides[get_db] = lambda: None
    user = SimpleNamespace(id=uuid4())
    org_id, bot_id = uuid4(), uuid4()
    path = f"/api/v1/organizations/{org_id}/bots/{bot_id}/voice/ice-servers"
    allowed = [False]

    async def get_bot(_db, org, bot, uid):
        assert (org, bot, uid) == (org_id, bot_id, user.id)
        if not allowed[0]:
            raise HTTPException(403, "Not a member")

    monkeypatch.setattr(bots_api.bots, "get_bot", get_bot)
    await env.manager.refresh()
    env.now[0] += constants.TURN_PROPAGATION_SECONDS
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        app.dependency_overrides[current_user] = lambda: user
        assert (await client.get(path)).status_code == 403
        allowed[0] = True
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["ice_servers"][1]["username"] == "temporary-user-1"
        assert "management-secret" not in response.text
        for _ in range(constants.TURN_REQUESTS_PER_MINUTE - 1):
            assert (await client.get(path)).status_code == 200
        assert (await client.get(path)).status_code == 429


@pytest.mark.asyncio
async def test_public_integration_requires_valid_unused_bot_bound_grant(
    env, monkeypatch
):
    app = FastAPI()
    app.include_router(public_api.router)
    app.state.turn = env.manager
    app.dependency_overrides[get_db] = lambda: None
    bot_id = uuid4()
    grant = SimpleNamespace(api_key_id=uuid4(), used_at=None, offer_expires_at=utcnow())
    from datetime import timedelta

    grant.offer_expires_at += timedelta(minutes=5)

    async def resolve(_db, bot, raw):
        if raw != "valid-grant" or bot != bot_id:
            raise HTTPException(401, "Invalid grant")
        return grant, None

    monkeypatch.setattr(public_api.public_voice, "resolve_grant", resolve)
    await env.manager.refresh()
    env.now[0] += constants.TURN_PROPAGATION_SECONDS
    path = f"/bots/{bot_id}/webrtc/ice-servers"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        headers = {"Authorization": "Bearer valid-grant"}
        assert (await client.get(path, headers=headers)).status_code == 200
        grant.used_at = utcnow()
        assert (await client.get(path, headers=headers)).status_code == 409
        grant.used_at = None
        grant.offer_expires_at = utcnow() - timedelta(seconds=1)
        assert (await client.get(path, headers=headers)).status_code == 409
