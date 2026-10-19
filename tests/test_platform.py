from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db.models import ApiKey, BotTemplate, VoiceGrant, utcnow
from app.main import public_app

TEMPLATE = "00000000-0000-4000-8000-000000000002"


async def account(c, email):
    response = await c.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Owner", "password": "correct-horse-123"},
    )
    assert response.status_code == 201
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    org = (await c.get("/api/v1/organizations", headers=headers)).json()[0]["id"]
    return headers, org


async def setup_bot(c):
    headers, org = await account(c, "owner@example.com")
    response = await c.post(
        f"/api/v1/organizations/{org}/bots",
        headers=headers,
        json={"template_id": TEMPLATE, "name": "Our Clinic", "company_context": "We open at 9am."},
    )
    assert response.status_code == 201, response.text
    key = await c.post(
        f"/api/v1/organizations/{org}/api-keys", headers=headers, json={"name": "Website"}
    )
    assert key.status_code == 201
    return headers, org, response.json()["id"], key.json()


async def test_template_prompt_is_platform_owned(client):
    c, sessions = client
    headers, org, bot, _ = await setup_bot(c)
    templates = (await c.get("/api/v1/bot-templates", headers=headers)).json()
    assert templates[0]["id"] == TEMPLATE
    assert "system_prompt" not in templates[0]
    base = f"/api/v1/organizations/{org}/bots/{bot}"
    result = await c.put(
        base, headers=headers, json={"name": "Changed", "system_prompt": "Replace platform rules"}
    )
    assert result.status_code == 422
    result = await c.put(
        base,
        headers=headers,
        json={"name": "Changed", "company_context": "Our services are dental."},
    )
    assert result.status_code == 200
    assert result.json()["template_id"] == TEMPLATE
    assert "system_prompt" not in result.json()
    async with sessions() as db:
        template = await db.get(BotTemplate, UUID(TEMPLATE))
        assert template.system_prompt == "Platform-owned clinic instructions"


async def test_keys_are_hashed_scoped_and_revocable(client):
    c, sessions = client
    owner, org, bot, key = await setup_bot(c)
    other, other_org = await account(c, "other@example.com")
    base = f"/api/v1/organizations/{org}/api-keys"
    listed = (await c.get(base, headers=owner)).json()
    assert "key" not in listed[0] and "token_hash" not in listed[0]
    assert (await c.get(base, headers=other)).status_code == 404
    async with sessions() as db:
        stored = await db.get(ApiKey, UUID(key["id"]))
        assert stored.token_hash != key["key"]
    foreign_key = (
        await c.post(
            f"/api/v1/organizations/{other_org}/api-keys", headers=other, json={"name": "Other"}
        )
    ).json()
    endpoint = f"/api/v1/public/bots/{bot}/session-token"
    assert (await c.post(endpoint, headers={"X-API-Key": foreign_key["key"]})).status_code == 404
    assert (await c.post(endpoint, headers=owner)).status_code == 401
    token = (await c.post(endpoint, headers={"X-API-Key": key["key"]})).json()["token"]
    assert (await c.delete(f"{base}/{key['id']}", headers=owner)).status_code == 204
    assert (await c.post(endpoint, headers={"X-API-Key": key["key"]})).status_code == 401
    assert (
        await c.post(
            f"/api/v1/public/bots/{bot}/webrtc/offer",
            headers={"Authorization": "Bearer " + token},
            json={"sdp": "fake"},
        )
    ).status_code == 401


async def test_public_voice_is_single_use_and_bot_bound(client):
    c, sessions = client
    _, org, bot, key = await setup_bot(c)
    token_response = await c.post(
        f"/api/v1/public/bots/{bot}/session-token", headers={"X-API-Key": key["key"]}
    )
    assert token_response.status_code == 201
    token = token_response.json()["token"]
    headers = {"Authorization": "Bearer " + token}
    manager = public_app.state.voice
    manager.offer = AsyncMock(return_value={"sdp": "answer", "type": "answer", "pc_id": "test-pc"})
    manager.stop = AsyncMock()
    assert (
        await c.post(
            f"/api/v1/public/bots/{uuid4()}/webrtc/offer", headers=headers, json={"sdp": "fake"}
        )
    ).status_code == 401
    endpoint = f"/api/v1/public/bots/{bot}/webrtc"
    assert (
        await c.post(endpoint + "/offer", headers=headers, json={"sdp": "fake"})
    ).status_code == 200
    runtime, principal, _ = manager.offer.call_args.args
    assert runtime.system_prompt == "Platform-owned clinic instructions"
    assert runtime.company_context == "We open at 9am."
    assert (
        await c.post(endpoint + "/offer", headers=headers, json={"sdp": "fake"})
    ).status_code == 409
    assert manager.offer.await_count == 1
    assert (await c.delete(endpoint + "/test-pc", headers=headers)).status_code == 204
    manager.stop.assert_awaited_once_with("test-pc", principal, UUID(org), UUID(bot))
    async with sessions() as db:
        grant = await db.scalar(select(VoiceGrant))
        assert grant.token_hash != token and grant.used_at is not None


async def test_expired_keys_and_voice_offer_windows(client):
    c, sessions = client
    _, _, bot, key = await setup_bot(c)
    endpoint = f"/api/v1/public/bots/{bot}"
    token = (await c.post(endpoint + "/session-token", headers={"X-API-Key": key["key"]})).json()[
        "token"
    ]
    async with sessions() as db:
        grant = await db.scalar(select(VoiceGrant))
        grant.offer_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (
        await c.post(
            endpoint + "/webrtc/offer",
            headers={"Authorization": "Bearer " + token},
            json={"sdp": "fake"},
        )
    ).status_code == 409
    async with sessions() as db:
        stored = await db.get(ApiKey, UUID(key["id"]))
        stored.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (
        await c.post(endpoint + "/session-token", headers={"X-API-Key": key["key"]})
    ).status_code == 401


async def test_inactive_templates_block_new_calls_and_public_cors(client):
    c, sessions = client
    _, _, bot, key = await setup_bot(c)
    async with sessions() as db:
        template = await db.get(BotTemplate, UUID(TEMPLATE))
        template.is_active = False
        await db.commit()
    endpoint = f"/api/v1/public/bots/{bot}"
    assert (
        await c.post(endpoint + "/session-token", headers={"X-API-Key": key["key"]})
    ).status_code == 404
    preflight = await c.options(
        endpoint + "/webrtc/offer",
        headers={
            "Origin": "https://customer.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
