"""Workspace scoping and permissions must hold without any frontend filtering."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from test_clinic import CONTEXT
from test_platform import TEMPLATE, account, setup_bot

from app.db.models import BotTemplate, Meeting, Membership, User, utcnow


async def test_workspace_overview_is_scoped_and_prepares_feature_lists(client):
    c, sessions = client
    owner, org, bot, _ = await setup_bot(c)
    outsider, other_org = await account(c, "outsider@example.com")
    member, _ = await account(c, "member@example.com")
    clinic_template = uuid4()
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.email == "member@example.com"))
        db.add(Membership(organization_id=UUID(org), user_id=user.id, role="member"))
        db.add(
            BotTemplate(
                id=clinic_template,
                slug="clinical",
                name="Clinical",
                description="Bookings",
                system_prompt="private-base-prompt",
                service_type="clinic",
            )
        )
        await db.commit()
    clinic = (
        await c.post(
            f"/api/v1/organizations/{org}/bots",
            headers=owner,
            json={
                "name": "Clinic",
                "template_id": str(clinic_template),
                "company_context": CONTEXT,
            },
        )
    ).json()
    other = (
        await c.post(
            f"/api/v1/organizations/{other_org}/bots",
            headers=outsider,
            json={
                "name": "Unrelated bot",
                "template_id": TEMPLATE,
            },
        )
    ).json()
    endpoint = f"/api/v1/organizations/{org}/workspace"
    response = await c.get(endpoint, headers=owner)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["organization"]["id"] == org
    assert data["role"] == "owner" and data["can_manage"] and data["can_invite_admins"]
    assert {item["id"] for item in data["bots"]} == {bot, clinic["id"]}
    assert [item["id"] for item in data["features"]["clinic"]["bots"]] == [clinic["id"]]
    assert [item["id"] for item in data["features"]["conversation"]["bots"]] == [bot]
    assert {item["service_type"] for item in data["features"]["clinic"]["templates"]} == {"clinic"}
    assert other["id"] not in response.text
    assert "private-base-prompt" not in response.text
    assert "token_hash" not in response.text and "password_hash" not in response.text
    assert (await c.get(endpoint, headers=outsider)).status_code == 404
    assert (await c.get(endpoint)).status_code == 401
    # Client-supplied permission hints do not affect server-derived capabilities.
    member_data = (await c.get(endpoint + "?can_manage=true&role=owner", headers=member)).json()
    assert member_data["role"] == "member"
    assert member_data["can_manage"] is False and member_data["can_invite_admins"] is False
    assert (
        await c.put(
            f"/api/v1/organizations/{org}/bots/{bot}",
            headers=member,
            json={"name": "Forged permission"},
        )
    ).status_code == 403


async def test_cross_workspace_requests_rejected_across_resource_families(client, monkeypatch):
    from app.api import meetings as meeting_api

    c, sessions = client
    owner, org, bot, key = await setup_bot(c)
    outsider, other_org = await account(c, "outsider@example.com")
    monkeypatch.setattr(meeting_api, "is_operator", lambda user: True)
    async with sessions() as db:
        meeting = Meeting(
            organization_id=UUID(org),
            occurrence_key="scoping-test",
            title="Private meeting",
            meet_url="https://meet.google.com/abc-defg-hij",
            starts_at=utcnow() + timedelta(hours=1),
            ends_at=utcnow() + timedelta(hours=2),
            timezone="UTC",
        )
        db.add(meeting)
        await db.commit()
        meeting_id = meeting.id
    root = f"/api/v1/organizations/{org}"
    requests = [
        ("GET", root + "/workspace", None),
        ("GET", root + "/members", None),
        ("POST", root + "/invitations", {"email": "invite@example.com"}),
        ("GET", root + "/api-keys", None),
        ("POST", root + "/api-keys", {"name": "Forged"}),
        ("DELETE", root + f"/api-keys/{key['id']}", None),
        ("GET", root + "/bots", None),
        ("GET", root + f"/bots/{bot}", None),
        ("GET", root + f"/bots/{bot}/clinic/info", None),
        ("GET", root + f"/bots/{bot}/clinic/patients", None),
        ("GET", root + f"/bots/{bot}/clinic/appointments?day=2026-10-19", None),
        ("PATCH", root + f"/bots/{bot}/clinic/appointments/{uuid4()}/visit", {"visited": True}),
        ("GET", root + "/meetings", None),
        ("GET", root + "/meeting-memory", None),
        ("GET", root + f"/meetings/{meeting_id}/connection", None),
        ("POST", root + f"/meetings/{meeting_id}/join", None),
        ("POST", root + f"/meetings/{meeting_id}/leave", None),
        ("PUT", root + f"/meetings/{meeting_id}/schedule", {"auto_join": True}),
    ]
    for method, path, body in requests:
        result = await c.request(method, path, headers=outsider, json=body)
        assert result.status_code == 404, (method, path, result.text)
    # Knowing a resource ID does not permit moving it under an authorized parent.
    for suffix in [f"/bots/{bot}", f"/meetings/{meeting_id}/connection"]:
        assert (
            await c.get(f"/api/v1/organizations/{other_org}" + suffix, headers=outsider)
        ).status_code == 404
    assert (
        await c.delete(f"/api/v1/organizations/{other_org}/api-keys/{key['id']}", headers=outsider)
    ).status_code == 404
    assert (await c.get(root + "/meetings", headers=owner)).status_code == 200


async def test_membership_removal_invalidates_workspace_access_without_relogin(client):
    from sqlalchemy import delete

    c, sessions = client
    _, org, _, _ = await setup_bot(c)
    member, _ = await account(c, "member@example.com")
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.email == "member@example.com"))
        user_id = user.id
        db.add(Membership(organization_id=UUID(org), user_id=user_id, role="admin"))
        await db.commit()
    endpoint = f"/api/v1/organizations/{org}/workspace"
    assert (await c.get(endpoint, headers=member)).json()["can_manage"] is True
    async with sessions() as db:
        await db.execute(
            delete(Membership).where(
                Membership.organization_id == UUID(org), Membership.user_id == user_id
            )
        )
        await db.commit()
    assert (await c.get(endpoint, headers=member)).status_code == 404
    assert org not in {
        item["id"] for item in (await c.get("/api/v1/organizations", headers=member)).json()
    }


async def test_dashboard_cors_supports_patient_visit_patch(client):
    from app.env import get_settings

    c, _ = client
    configured = get_settings().cors_origins
    origin = next((value for value in configured if value != "*"), "http://localhost:3000")
    preflight = await c.options(
        "/api/v1/organizations/test/bots/test/clinic/appointments/test/visit",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert "PATCH" in preflight.headers["access-control-allow-methods"]
    public = await c.options(
        "/api/v1/public/bots/test/webrtc/offer",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PATCH",
        },
    )
    assert public.status_code == 400
