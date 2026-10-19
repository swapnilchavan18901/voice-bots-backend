import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from test_platform import TEMPLATE, setup_bot

from app.business import clinic
from app.business.clinic_context import details
from app.db.models import BotTemplate, ClinicDoctor

CONTEXT = json.dumps(
    {
        "background": "Our clinic treats families.",
        "clinic": {
            "name": "Customer Clinic",
            "phone": "020 55550000",
            "fees": "Consultation ₹850",
            "timezone": "Asia/Kolkata",
            "working_days": [0, 1, 2, 3, 4, 5],
            "open_time": "10:00",
            "close_time": "18:00",
            "lunch_start": "14:00",
            "lunch_end": "15:00",
            "slot_minutes": 30,
            "doctors": [{"name": "Dr. Customer", "specialty": "Family medicine"}],
        },
    }
)
PROFILE = details(CONTEXT)


async def prepare(client):
    c, sessions = client
    headers, org, bot, _ = await setup_bot(c)
    async with sessions() as db:
        template = await db.get(BotTemplate, UUID(TEMPLATE))
        template.service_type = "clinic"
        template.name = "Dynamic renamed template"
        db.add(ClinicDoctor(id=1, name="Dr. Anjali Mehta", specialty="General Physician"))
        await db.commit()
    result = await c.put(
        f"/api/v1/organizations/{org}/bots/{bot}",
        headers=headers,
        json={"name": "Customer receptionist", "company_context": CONTEXT},
    )
    assert result.status_code == 200, result.text
    day = clinic.now_local(PROFILE).date() + timedelta(days=1)
    while not clinic.slots(day, PROFILE):
        day += timedelta(days=1)
    base = f"/api/v1/organizations/{org}/bots/{bot}/clinic"
    return c, sessions, headers, org, bot, base, day


async def test_booking_lifecycle_isolation_and_validation(client):
    c, sessions, headers, org, bot, base, day = await prepare(client)
    info = await c.get(base + "/info", headers=headers)
    assert info.json()["clinic_name"] == "Customer Clinic"
    assert info.json()["phone"] == "020 55550000"
    assert "Sunrise" not in info.text
    doctor_id = info.json()["doctors"][0]["id"]
    assert doctor_id != 1  # legacy shared doctor is not exposed or bookable
    slot = clinic.slots(day, PROFILE)[0].isoformat()
    data = dict(
        doctor_id=doctor_id, patient_name="Caller", patient_phone="+91 98765 43210", start_time=slot
    )
    booked = await c.post(base + "/appointments", headers=headers, json=data)
    assert booked.status_code == 201, booked.text
    appointment = booked.json()["id"]
    assert (await c.post(base + "/appointments", headers=headers, json=data)).status_code == 409
    free = (await c.get(base + "/availability", headers=headers, params={"day": str(day)})).json()
    assert slot not in free["doctors"][0]["slots"]
    for bad in [
        dict(data, start_time=f"{day}T14:00:00"),
        dict(data, start_time=f"{day}T09:00:00"),
        dict(data, patient_phone="abc"),
        dict(data, doctor_id=999),
        dict(data, patient_name="   "),
    ]:
        assert (await c.post(base + "/appointments", headers=headers, json=bad)).status_code in (
            404,
            422,
        )
    other = (
        await c.post(
            f"/api/v1/organizations/{org}/bots",
            headers=headers,
            json={"name": "Other", "template_id": TEMPLATE, "company_context": CONTEXT},
        )
    ).json()["id"]
    other_base = base.replace(bot, other)
    other_id = (await c.get(other_base + "/info", headers=headers)).json()["doctors"][0]["id"]
    assert other_id != doctor_id
    assert (
        await c.post(other_base + "/appointments", headers=headers, json=data)
    ).status_code == 404
    assert (
        await c.post(
            other_base + "/appointments", headers=headers, json=dict(data, doctor_id=other_id)
        )
    ).status_code == 201
    change = {"appointment_id": appointment, "patient_phone": "9876543210"}
    assert (
        await c.post(other_base + "/appointments/cancel", headers=headers, json=change)
    ).status_code == 404
    assert (
        await c.post(
            base + "/appointments/cancel",
            headers=headers,
            json=dict(change, patient_phone="1234567890"),
        )
    ).status_code == 404
    moved = await c.post(
        base + "/appointments/reschedule",
        headers=headers,
        json=dict(change, new_start_time=clinic.slots(day, PROFILE)[1].isoformat()),
    )
    assert moved.status_code == 200
    assert (
        await c.post(base + "/appointments/cancel", headers=headers, json=change)
    ).status_code == 200
    assert (
        await c.post(
            base + "/appointments/lookup", headers=headers, json={"patient_phone": "9876543210"}
        )
    ).json() == []
    assert (await c.post(base + "/appointments", headers=headers, json=data)).status_code == 201


async def test_reference_flow_requires_a_new_affirmative_turn(client):
    from app.ai.clinic.flow import handlers, nodes
    from app.business.bots import runtime_bot
    from app.db.models import Bot

    c, sessions, headers, org, bot, base, day = await prepare(client)
    async with sessions() as db:
        runtime = await runtime_bot(db, await db.get(Bot, UUID(bot)))
    state = {
        "bot": runtime,
        "facts": runtime.clinic_facts,
        "session_factory": sessions,
        "caller_turn": 1,
        "last_caller_text": "My number is 9876543210",
    }
    fm = SimpleNamespace(
        state=state, current_node="greet_identify", worker=SimpleNamespace(queue_frame=AsyncMock())
    )
    result, step = await handlers.save_caller({"name": "Caller", "phone": "9876543210"}, fm)
    assert result["ok"] and step["name"] == "intent"
    result, step = await handlers.choose_book({}, fm)
    assert step["name"] == "collect_slot"
    doctor_id = runtime.clinic_facts["doctors"][0]["id"]
    result, step = await handlers.select_slot(
        {
            "doctor_id": doctor_id,
            "start_time": clinic.slots(day, PROFILE)[0].isoformat(),
            "reason": "Fever",
        },
        fm,
    )
    assert result["ok"] and step["name"] == "confirm_booking"
    fm.current_node = "confirm_booking"
    state["last_caller_text"] = "yes"
    result, _ = await handlers.confirm_booking({}, fm)
    assert result["ok"] is False  # same turn cannot self-confirm
    state.update(caller_turn=2, last_caller_text="No, wait")
    assert (await handlers.confirm_booking({}, fm))[0]["ok"] is False
    state.update(caller_turn=3, last_caller_text="Yes please")
    result, step = await handlers.confirm_booking({}, fm)
    assert result["ok"] and step["name"] == "close"
    found = await c.post(
        base + "/appointments/lookup", headers=headers, json={"patient_phone": "9876543210"}
    )
    assert len(found.json()) == 1
    fm.current_node = "close"
    assert (await handlers.confirm_booking({}, fm))[0]["ok"] is False
    prompt = nodes.greet_identify(state)["role_message"]
    assert "Customer Clinic" in prompt and "Sunrise" not in prompt
    assert runtime.template_name == "Dynamic renamed template"


async def test_staff_request_waits_for_caller_and_does_not_claim_delivery(client):
    from app.ai.clinic.flow import handlers
    from app.business.bots import runtime_bot
    from app.db.models import Bot

    c, sessions, headers, org, bot, base, day = await prepare(client)
    async with sessions() as db:
        runtime = await runtime_bot(db, await db.get(Bot, UUID(bot)))
    state = {
        "bot": runtime,
        "facts": runtime.clinic_facts,
        "caller_turn": 1,
        "last_caller_text": "I would like to book",
    }
    fm = SimpleNamespace(state=state, current_node="collect_slot")
    result, step = await handlers.transfer_to_human({"reason": "Needs staff"}, fm)
    assert step["name"] == "confirm_transfer"
    fm.current_node = "confirm_transfer"
    assert (await handlers.confirm_transfer({}, fm))[0]["ok"] is False
    state.update(caller_turn=2, last_caller_text="No")
    assert (await handlers.confirm_transfer({}, fm))[0]["ok"] is False
    assert "handoff" not in state
    state.update(caller_turn=3, last_caller_text="Yes")
    result, step = await handlers.confirm_transfer({}, fm)
    assert result["request_logged"] and result["staff_notified"] is False
    assert step["name"] == "transfer"


async def test_context_updates_and_missing_data_do_not_use_defaults(client):
    c, sessions, headers, org, bot, base, day = await prepare(client)
    endpoint = base.removesuffix("/clinic")
    text_context = "We are Maple Clinic. We charge 900 rupees and open Sundays."
    response = await c.put(
        endpoint, headers=headers, json={"name": "Maple", "company_context": text_context}
    )
    assert response.status_code == 200
    info = (await c.get(base + "/info", headers=headers)).json()
    assert info["company_context"] == text_context
    assert info["background"] == text_context
    assert info["doctors"] == []
    assert info["clinic_name"] is None
    assert "phone" not in info and "fees" not in info and "hours" not in info
    assert (
        await c.get(base + "/availability", headers=headers, params={"day": str(day)})
    ).status_code == 422
    changed = json.loads(CONTEXT)
    changed["clinic"]["name"] = "Updated Clinic"
    changed["clinic"]["fees"] = "Consultation ₹1200"
    changed["clinic"]["open_time"] = "11:00"
    changed["clinic"]["doctors"][0]["specialty"] = "Internal medicine"
    response = await c.put(
        endpoint, headers=headers, json={"name": "Updated", "company_context": json.dumps(changed)}
    )
    assert response.status_code == 200
    info = (await c.get(base + "/info", headers=headers)).json()
    assert info["clinic_name"] == "Updated Clinic" and info["fees"] == "Consultation ₹1200"
    assert info["doctors"][0]["specialty"] == "Internal medicine"
    free = (await c.get(base + "/availability", headers=headers, params={"day": str(day)})).json()
    assert free["doctors"][0]["slots"][0].endswith("11:00:00")
    changed["clinic"]["timezone"] = "Invalid/Timezone"
    assert (
        await c.put(
            endpoint, headers=headers, json={"name": "Bad", "company_context": json.dumps(changed)}
        )
    ).status_code == 422


async def test_markdown_published_slots_are_doctor_specific(client, monkeypatch):
    from datetime import date, datetime
    from pathlib import Path

    c, sessions, headers, org, bot, base, day = await prepare(client)
    context = (Path(__file__).parent / "fixtures/markdown_clinic.md").read_text()
    monkeypatch.setattr(clinic, "now_local", lambda profile=None: datetime(2026, 10, 13, 9))
    updated = await c.put(
        base.removesuffix("/clinic"),
        headers=headers,
        json={"name": "Asta", "company_context": context},
    )
    assert updated.status_code == 200, updated.text
    info = (await c.get(base + "/info", headers=headers)).json()
    assert info["clinic_name"] == "Seaview Family Clinic"
    assert info["phone"] == "020 55550000"  # sample patient's contact is not clinic contact
    assert info["booking_ready"] is True
    assert len(info["doctors"]) == 3
    physician = next(d for d in info["doctors"] if d["specialty"] == "General Physician")
    pediatrician = next(d for d in info["doctors"] if d["specialty"] == "Pediatrician")
    day = date(2026, 10, 14)
    result = (
        await c.get(
            base + "/availability",
            headers=headers,
            params={"day": str(day), "doctor_id": physician["id"]},
        )
    ).json()
    assert result["doctors"][0]["slots"] == [
        f"{day}T10:00:00",
        f"{day}T11:20:00",
        f"{day}T17:00:00",
    ]
    request = {
        "doctor_id": physician["id"],
        "patient_name": "Caller",
        "patient_phone": "9876543210",
        "start_time": f"{day}T12:00:00",
    }
    assert (await c.post(base + "/appointments", headers=headers, json=request)).status_code == 422
    # Noon exists only for the pediatrician, not every doctor in that clinic.
    assert (
        await c.post(
            base + "/appointments",
            headers=headers,
            json={**request, "doctor_id": pediatrician["id"]},
        )
    ).status_code == 201
    assert (
        await c.post(
            base + "/appointments",
            headers=headers,
            json={**request, "start_time": f"{day}T11:20:00"},
        )
    ).status_code == 201
    result = (
        await c.get(
            base + "/availability",
            headers=headers,
            params={"day": str(day), "doctor_id": physician["id"]},
        )
    ).json()
    assert f"{day}T11:20:00" not in result["doctors"][0]["slots"]
    # An advertised one-off date does not turn into a fabricated recurring schedule.
    result = (
        await c.get(
            base + "/availability",
            headers=headers,
            params={"day": "2026-10-15", "doctor_id": physician["id"]},
        )
    ).json()
    assert result["doctors"][0]["slots"] == []
    # Saving through the editor wrapper preserves dated slots from Markdown background.
    wrapped = json.dumps(
        {
            "background": context,
            "clinic": {
                "doctors": [
                    {"name": d["name"], "specialty": d["specialty"]} for d in info["doctors"]
                ]
            },
        }
    )
    assert (
        details(wrapped).doctors[0].available_slots == details(context).doctors[0].available_slots
    )


async def test_daily_register_visit_persistence_and_isolation(client, monkeypatch):
    from datetime import datetime

    from sqlalchemy import select
    from test_platform import account

    from app.db.models import ClinicAppointment, Membership, User

    c, sessions, headers, org, bot, base, day = await prepare(client)
    doctor_id = (await c.get(base + "/info", headers=headers)).json()["doctors"][0]["id"]
    booked = await c.post(
        base + "/appointments",
        headers=headers,
        json={
            "doctor_id": doctor_id,
            "patient_name": "Daily Patient",
            "patient_phone": "9876543210",
            "start_time": clinic.slots(day, PROFILE)[0].isoformat(),
        },
    )
    assert booked.status_code == 201, booked.text
    appointment_id = booked.json()["id"]
    endpoint = base + f"/appointments/{appointment_id}/visit"
    assert (await c.get(base + "/appointments", headers=headers)).status_code == 422
    assert (
        await c.get(
            base + "/appointments", headers=headers, params={"day": str(day + timedelta(days=1))}
        )
    ).json() == []
    rows = (await c.get(base + "/appointments", headers=headers, params={"day": str(day)})).json()
    assert len(rows) == 1
    assert rows[0]["doctor_name"] == "Dr. Customer"
    assert rows[0]["visited"] is False
    assert (await c.patch(endpoint, headers=headers, json={"visited": True})).status_code == 409
    monkeypatch.setattr(
        clinic, "now_local", lambda profile=None: datetime.combine(day, datetime.min.time())
    )
    result = await c.patch(endpoint, headers=headers, json={"visited": True})
    assert result.status_code == 200, result.text
    async with sessions() as db:
        saved = await db.get(ClinicAppointment, UUID(appointment_id))
        assert saved.visited is True and saved.status == "booked"
    rows = (await c.get(base + "/appointments", headers=headers, params={"day": str(day)})).json()
    assert rows[0]["visited"] is True
    # A completed visit must not free an occupied booking slot.
    availability = (
        await c.get(base + "/availability", headers=headers, params={"day": str(day)})
    ).json()
    assert rows[0]["start_time"] not in availability["doctors"][0]["slots"]
    assert (await c.patch(endpoint, headers=headers, json={"visited": False})).json()[
        "visited"
    ] is False
    other = (
        await c.post(
            f"/api/v1/organizations/{org}/bots",
            headers=headers,
            json={
                "name": "Other clinic",
                "template_id": TEMPLATE,
                "company_context": CONTEXT,
            },
        )
    ).json()["id"]
    other_base = base.replace(bot, other)
    assert (
        await c.get(other_base + "/appointments", headers=headers, params={"day": str(day)})
    ).json() == []
    assert (
        await c.patch(endpoint.replace(bot, other), headers=headers, json={"visited": True})
    ).status_code == 404
    outsider, _ = await account(c, "outsider@example.com")
    assert (
        await c.get(base + "/appointments", headers=outsider, params={"day": str(day)})
    ).status_code == 404
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.email == "outsider@example.com"))
        db.add(Membership(organization_id=UUID(org), user_id=user.id, role="member"))
        await db.commit()
    assert (
        await c.get(base + "/appointments", headers=outsider, params={"day": str(day)})
    ).status_code == 200
    assert (await c.patch(endpoint, headers=outsider, json={"visited": True})).status_code == 403
    async with sessions() as db:
        saved = await db.get(ClinicAppointment, UUID(appointment_id))
        saved.status = "cancelled"
        await db.commit()
    assert (await c.patch(endpoint, headers=headers, json={"visited": True})).status_code == 409


async def test_patient_register_counts_dates_and_permissions_are_server_prepared(
    client, monkeypatch
):
    from datetime import datetime

    from sqlalchemy import select
    from test_platform import account

    from app.db.models import Membership, User

    c, sessions, headers, org, bot, base, day = await prepare(client)
    doctor_id = (await c.get(base + "/info", headers=headers)).json()["doctors"][0]["id"]
    appointment = (
        await c.post(
            base + "/appointments",
            headers=headers,
            json={
                "doctor_id": doctor_id,
                "patient_name": "Registered",
                "patient_phone": "9876543210",
                "start_time": clinic.slots(day, PROFILE)[0].isoformat(),
            },
        )
    ).json()
    endpoint = base + "/patients"
    result = (await c.get(endpoint, headers=headers, params={"day": str(day)})).json()
    assert (
        result["day"] == str(day)
        and result["registered_count"] == 1
        and result["visited_count"] == 0
    )
    assert result["patients"][0]["can_update_visit"] is False  # future day
    monkeypatch.setattr(
        clinic, "now_local", lambda profile=None: datetime.combine(day, datetime.min.time())
    )
    today = (await c.get(endpoint, headers=headers)).json()
    assert today["day"] == str(day) and today["patients"][0]["can_update_visit"] is True
    await c.patch(
        base + f"/appointments/{appointment['id']}/visit", headers=headers, json={"visited": True}
    )
    assert (await c.get(endpoint, headers=headers)).json()["visited_count"] == 1
    member, _ = await account(c, "member@example.com")
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.email == "member@example.com"))
        db.add(Membership(organization_id=UUID(org), user_id=user.id, role="member"))
        await db.commit()
    readonly = (await c.get(endpoint, headers=member)).json()
    assert readonly["registered_count"] == 1
    assert readonly["patients"][0]["can_update_visit"] is False
    assert (
        await c.patch(
            base + f"/appointments/{appointment['id']}/visit",
            headers=member,
            json={"visited": False},
        )
    ).status_code == 403
