from datetime import date
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import DB, CurrentUser
from app.business import bots, clinic
from app.business.clinic_context import sync_doctors
from app.business.organizations import MANAGE_ROLES, require_membership
from app.db.models import Bot
from app.schemas.clinic import (
    Booking,
    Caller,
    ChangeAppointment,
    RegisterOut,
    Reschedule,
    VisitUpdate,
)

router = APIRouter(prefix="/organizations/{org_id}/bots/{bot_id}/clinic", tags=["clinic"])


async def authorize(db, org_id, bot_id, user, write=False):
    bot = await bots.get_bot(db, org_id, bot_id, user.id, write=write)
    await clinic.require_clinic(db, bot_id)
    await db.execute(select(Bot).where(Bot.id == bot_id).with_for_update())
    await db.refresh(bot)
    await sync_doctors(db, bot)
    await db.commit()


@router.get("/info")
async def info(org_id: UUID, bot_id: UUID, db: DB, user: CurrentUser):
    await authorize(db, org_id, bot_id, user)
    return await clinic.clinic_info(db, bot_id)


@router.get("/availability")
async def availability(
    org_id: UUID, bot_id: UUID, day: date, db: DB, user: CurrentUser, doctor_id: int | None = None
):
    await authorize(db, org_id, bot_id, user)
    return await clinic.availability(db, bot_id, day, doctor_id)


@router.post("/appointments", status_code=201)
async def book(org_id: UUID, bot_id: UUID, data: Booking, db: DB, user: CurrentUser):
    await authorize(db, org_id, bot_id, user, write=True)
    return await clinic.book(db, bot_id, data)


@router.post("/appointments/lookup")
async def lookup(org_id: UUID, bot_id: UUID, data: Caller, db: DB, user: CurrentUser):
    await authorize(db, org_id, bot_id, user)
    return await clinic.upcoming(db, bot_id, data.patient_phone)


@router.post("/appointments/reschedule")
async def reschedule(org_id: UUID, bot_id: UUID, data: Reschedule, db: DB, user: CurrentUser):
    await authorize(db, org_id, bot_id, user, write=True)
    return await clinic.change(db, bot_id, data)


@router.post("/appointments/cancel")
async def cancel(org_id: UUID, bot_id: UUID, data: ChangeAppointment, db: DB, user: CurrentUser):
    await authorize(db, org_id, bot_id, user, write=True)
    return await clinic.change(db, bot_id, data, cancel=True)


@router.get("/appointments")
async def daily_patients(org_id: UUID, bot_id: UUID, day: date, db: DB, user: CurrentUser):
    await bots.get_bot(db, org_id, bot_id, user.id)
    await clinic.require_clinic(db, bot_id)
    return await clinic.daily_patients(db, bot_id, day)


@router.patch("/appointments/{appointment_id}/visit")
async def visit(
    org_id: UUID,
    bot_id: UUID,
    appointment_id: UUID,
    data: VisitUpdate,
    db: DB,
    user: CurrentUser,
):
    await bots.get_bot(db, org_id, bot_id, user.id, write=True)
    await clinic.require_clinic(db, bot_id)
    return await clinic.set_visit(db, bot_id, appointment_id, data.visited)


@router.get("/patients", response_model=RegisterOut)
async def patient_register(
    org_id: UUID,
    bot_id: UUID,
    db: DB,
    user: CurrentUser,
    day: date | None = None,
):
    await bots.get_bot(db, org_id, bot_id, user.id)
    await clinic.require_clinic(db, bot_id)
    membership = await require_membership(db, org_id, user.id)
    return await clinic.daily_register(db, bot_id, membership.role in MANAGE_ROLES, day)
