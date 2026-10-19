from datetime import timedelta

from sqlalchemy import select

from app.db.models import Invitation, utcnow


async def register(client, email):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-123", "name": "Test User"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "password_hash" not in body["user"]
    headers = {"Authorization": "Bearer " + body["access_token"]}
    orgs = (await client.get("/api/v1/organizations", headers=headers)).json()
    assert len(orgs) == 1
    assert orgs[0]["name"] == email.lower().split("@")[0]
    return headers, orgs[0]["id"]


async def test_auth_and_atomic_personal_org(client):
    c, _ = client
    headers, org = await register(c, "Alice@example.com")
    members = (await c.get(f"/api/v1/organizations/{org}/members", headers=headers)).json()
    assert len(members) == 1 and members[0]["role"] == "owner"
    assert (
        await c.post(
            "/api/v1/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correct-horse-123",
                "name": "Duplicate",
            },
        )
    ).status_code == 409
    assert len((await c.get("/api/v1/organizations", headers=headers)).json()) == 1
    assert (
        await c.post(
            "/api/v1/auth/login",
            json={"email": "ALICE@example.com", "password": "correct-horse-123"},
        )
    ).status_code == 200
    assert (
        await c.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "incorrect-password"},
        )
    ).status_code == 401
    assert (await c.get("/api/v1/auth/me")).status_code == 401
    assert (
        await c.get("/api/v1/auth/me", headers={"Authorization": "Bearer invalid"})
    ).status_code == 401


async def test_invitation_is_email_bound_single_use_and_multi_org(client):
    c, _ = client
    owner, org = await register(c, "owner@example.com")
    guest, _ = await register(c, "guest@example.com")
    wrong, _ = await register(c, "wrong@example.com")
    response = await c.post(
        f"/api/v1/organizations/{org}/invitations",
        headers=owner,
        json={"email": "GUEST@example.com"},
    )
    assert response.status_code == 201
    token = response.json()["token"]
    assert (
        await c.post("/api/v1/invitations/accept", headers=wrong, json={"token": token})
    ).status_code == 403
    assert (
        await c.post("/api/v1/invitations/accept", headers=guest, json={"token": token})
    ).status_code == 200
    assert len((await c.get("/api/v1/organizations", headers=guest)).json()) == 2
    assert (
        await c.post("/api/v1/invitations/accept", headers=guest, json={"token": token})
    ).status_code == 400
    assert (
        await c.post(
            f"/api/v1/organizations/{org}/invitations",
            headers=guest,
            json={"email": "another@example.com"},
        )
    ).status_code == 403


async def test_expired_invite(client):
    c, sessions = client
    owner, org = await register(c, "owner@example.com")
    guest, _ = await register(c, "guest@example.com")
    invite = (
        await c.post(
            f"/api/v1/organizations/{org}/invitations",
            headers=owner,
            json={"email": "guest@example.com"},
        )
    ).json()
    async with sessions() as db:
        row = await db.scalar(select(Invitation))
        assert row.token_hash != invite["token"]
        row.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (
        await c.post("/api/v1/invitations/accept", headers=guest, json={"token": invite["token"]})
    ).status_code == 400


async def test_bots_are_tenant_scoped(client):
    c, _ = client
    owner, org = await register(c, "owner@example.com")
    outsider, other_org = await register(c, "outsider@example.com")
    base = f"/api/v1/organizations/{org}/bots"
    bot = await c.post(
        base,
        headers=owner,
        json={"name": "Hindi assistant", "template_id": "00000000-0000-4000-8000-000000000002"},
    )
    assert bot.status_code == 201
    bot_id = bot.json()["id"]
    assert (await c.get(base, headers=outsider)).status_code == 404
    assert (
        await c.get(f"/api/v1/organizations/{other_org}/bots/{bot_id}", headers=outsider)
    ).status_code == 404
    assert (
        await c.put(f"{base}/{bot_id}", headers=outsider, json={"name": "Stolen"})
    ).status_code == 404
    assert (
        await c.post(f"{base}/{bot_id}/voice/offer", headers=outsider, json={"sdp": "fake"})
    ).status_code == 404
    assert (
        await c.post(f"{base}/{bot_id}/voice/offer", headers=owner, json={"sdp": "fake"})
    ).status_code == 503
    assert (await c.put(f"{base}/{bot_id}", headers=owner, json={"name": "Updated"})).json()[
        "name"
    ] == "Updated"
    assert (await c.delete(f"{base}/{bot_id}", headers=owner)).status_code == 204
    assert (await c.get(f"{base}/{bot_id}", headers=owner)).status_code == 404


async def test_members_can_read_but_not_modify_bots(client):
    c, _ = client
    owner, org = await register(c, "owner@example.com")
    guest, _ = await register(c, "guest@example.com")
    invitation = (
        await c.post(
            f"/api/v1/organizations/{org}/invitations",
            headers=owner,
            json={"email": "guest@example.com"},
        )
    ).json()
    await c.post("/api/v1/invitations/accept", headers=guest, json={"token": invitation["token"]})
    base = f"/api/v1/organizations/{org}/bots"
    assert (await c.get(base, headers=guest)).status_code == 200
    assert (
        await c.post(
            base,
            headers=guest,
            json={"name": "Unauthorized", "template_id": "00000000-0000-4000-8000-000000000002"},
        )
    ).status_code == 403
