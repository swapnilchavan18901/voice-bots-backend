"""Fixed clinic capabilities, shared by HTTP and voice; all records belong to a bot."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.business.clinic_context import background, details
from app.db.models import Bot, BotTemplate, ClinicAppointment, ClinicDoctor, utcnow

TZ = ZoneInfo("Asia/Kolkata")


def now_local(profile=None):
    # Legacy appointment timestamps used India local time. New schedules specify their timezone.
    tz = ZoneInfo(profile.timezone) if profile and profile.timezone else TZ
    return datetime.now(tz).replace(tzinfo=None)


def slots(day, profile):
    profile.calendar()
    if any(d.available_slots is not None for d in profile.doctors):
        return sorted(
            {
                value
                for d in profile.doctors
                for value in (d.available_slots or [])
                if value.date() == day
            }
        )
    if day.weekday() not in profile.working_days:
        return []
    start, end = datetime.combine(day, profile.open_time), datetime.combine(day, profile.close_time)
    step = timedelta(minutes=profile.slot_minutes)
    result = []
    while start + step <= end:
        lunch_overlap = profile.lunch_start and (
            start < datetime.combine(day, profile.lunch_end)
            and start + step > datetime.combine(day, profile.lunch_start)
        )
        if not lunch_overlap:
            result.append(start)
        start += step
    return result


def doctor_slots(day, profile, doctor_name):
    chosen = next(d for d in profile.doctors if d.name == doctor_name)
    if chosen.available_slots is not None:
        return sorted(value for value in chosen.available_slots if value.date() == day)
    return slots(day, profile)


def validate_slot(value, profile, doctor_name):
    profile.calendar()
    if value.tzinfo:
        value = value.astimezone(ZoneInfo(profile.timezone)).replace(tzinfo=None)
    if value <= now_local(profile) or value not in doctor_slots(value.date(), profile, doctor_name):
        raise HTTPException(
            422, "Choose a future slot within the clinic schedule in this agent's context"
        )
    return value


async def profile_for(db, bot_id):
    bot = await db.get(Bot, bot_id)
    return details(bot.company_context)


async def require_clinic(db, bot_id):
    enabled = await db.scalar(
        select(Bot.id)
        .join(BotTemplate)
        .where(
            Bot.id == bot_id, BotTemplate.service_type == "clinic", BotTemplate.is_active.is_(True)
        )
    )
    if not enabled:
        raise HTTPException(404, "Clinic service is unavailable for this bot")


async def doctors(db, bot_id):
    profile = await profile_for(db, bot_id)
    names = [d.name for d in profile.doctors]
    return [
        {"id": d.id, "name": d.name, "specialty": d.specialty}
        for d in (
            await db.scalars(
                select(ClinicDoctor)
                .where(ClinicDoctor.bot_id == bot_id, ClinicDoctor.name.in_(names))
                .order_by(ClinicDoctor.id)
            )
        ).all()
    ]


async def doctor(db, bot_id, doctor_id):
    match = next((d for d in await doctors(db, bot_id) if d["id"] == doctor_id), None)
    if match is None:
        raise HTTPException(404, "Doctor not found in this agent's clinic context")
    return match


async def availability(db, bot_id, day: date, doctor_id=None):
    await require_clinic(db, bot_id)
    if doctor_id is not None:
        await doctor(db, bot_id, doctor_id)
    profile = await profile_for(db, bot_id)
    profile.calendar()
    catalog = await doctors(db, bot_id)
    if not catalog:
        raise HTTPException(422, "Add doctors to this agent's clinic context before booking")
    taken = set(
        (
            await db.execute(
                select(ClinicAppointment.doctor_id, ClinicAppointment.start_time).where(
                    ClinicAppointment.bot_id == bot_id,
                    ClinicAppointment.status == "booked",
                    ClinicAppointment.start_time >= datetime.combine(day, time.min),
                    ClinicAppointment.start_time
                    < datetime.combine(day + timedelta(days=1), time.min),
                )
            )
        ).all()
    )
    return {
        "date": day.isoformat(),
        "closed": not slots(day, profile),
        "doctors": [
            {
                **d,
                "slots": [
                    s.isoformat()
                    for s in doctor_slots(day, profile, d["name"])
                    if s > now_local(profile) and (d["id"], s) not in taken
                ],
            }
            for d in catalog
            if doctor_id is None or d["id"] == doctor_id
        ],
    }


def output(a):
    return {
        key: getattr(a, key)
        for key in (
            "id",
            "doctor_id",
            "patient_name",
            "patient_phone",
            "start_time",
            "reason",
            "status",
            "visited",
        )
    }


async def save(db, appointment):
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Slot is already booked") from None
    return output(appointment)


async def book(db, bot_id, data):
    await require_clinic(db, bot_id)
    chosen = await doctor(db, bot_id, data.doctor_id)
    values = data.model_dump()
    values["start_time"] = validate_slot(
        data.start_time, await profile_for(db, bot_id), chosen["name"]
    )
    appointment = ClinicAppointment(bot_id=bot_id, **values)
    db.add(appointment)
    return await save(db, appointment)


async def upcoming(db, bot_id, phone):
    await require_clinic(db, bot_id)
    return [
        output(a)
        for a in (
            await db.scalars(
                select(ClinicAppointment)
                .where(
                    ClinicAppointment.bot_id == bot_id,
                    ClinicAppointment.patient_phone == phone,
                    ClinicAppointment.status == "booked",
                    ClinicAppointment.start_time > now_local(await profile_for(db, bot_id)),
                )
                .order_by(ClinicAppointment.start_time)
            )
        ).all()
    ]


async def change(db, bot_id, data, cancel=False):
    await require_clinic(db, bot_id)
    appointment = await db.scalar(
        select(ClinicAppointment)
        .where(
            ClinicAppointment.id == data.appointment_id,
            ClinicAppointment.bot_id == bot_id,
            ClinicAppointment.patient_phone == data.patient_phone,
        )
        .with_for_update()
    )
    if appointment is None:
        raise HTTPException(404, "Appointment not found for this caller")
    if appointment.visited:
        raise HTTPException(409, "Remove the doctor visit mark before changing this appointment")
    if appointment.status != "booked" or appointment.start_time <= now_local(
        await profile_for(db, bot_id)
    ):
        raise HTTPException(409, "Only upcoming booked appointments can be changed")
    if cancel:
        appointment.status = "cancelled"
    else:
        doctor_id = data.doctor_id or appointment.doctor_id
        chosen = await doctor(db, bot_id, doctor_id)
        appointment.doctor_id = doctor_id
        appointment.start_time = validate_slot(
            data.new_start_time, await profile_for(db, bot_id), chosen["name"]
        )
    appointment.updated_at = utcnow()
    return await save(db, appointment)


async def clinic_info(db, bot_id):
    bot = await db.get(Bot, bot_id)
    profile = details(bot.company_context)
    facts = profile.model_dump(mode="json", exclude_none=True, exclude={"doctors"})
    facts["clinic_name"] = facts.pop("name", None)
    if profile.open_time and profile.close_time and profile.working_days:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        facts["hours"] = (
            ", ".join(days[d] for d in profile.working_days)
            + f" {profile.open_time:%H:%M}–{profile.close_time:%H:%M}"
        )
    if profile.lunch_start and profile.lunch_end:
        facts["lunch"] = f"{profile.lunch_start:%H:%M}–{profile.lunch_end:%H:%M}"
    catalog = await doctors(db, bot_id)
    try:
        profile.calendar()
        booking_ready = bool(catalog)
    except HTTPException:
        booking_ready = False
    return {
        **facts,
        "booking_ready": booking_ready,
        "today": now_local(profile).date().isoformat(),
        "company_context": bot.company_context,
        "background": background(bot.company_context),
        "doctors": catalog,
    }


async def daily_patients(db, bot_id, day):
    rows = await db.execute(
        select(ClinicAppointment, ClinicDoctor.name)
        .join(ClinicDoctor, ClinicDoctor.id == ClinicAppointment.doctor_id)
        .where(
            ClinicAppointment.bot_id == bot_id,
            ClinicAppointment.start_time >= datetime.combine(day, time.min),
            ClinicAppointment.start_time < datetime.combine(day + timedelta(days=1), time.min),
        )
        .order_by(ClinicAppointment.start_time, ClinicAppointment.created_at)
    )
    return [{**output(a), "doctor_name": name} for a, name in rows.all()]


async def set_visit(db, bot_id, appointment_id, visited):
    appointment = await db.scalar(
        select(ClinicAppointment)
        .where(
            ClinicAppointment.id == appointment_id,
            ClinicAppointment.bot_id == bot_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise HTTPException(404, "Appointment not found")
    if appointment.status != "booked":
        raise HTTPException(409, "Cancelled appointments cannot be marked as visited")
    if visited and appointment.start_time.date() > now_local(await profile_for(db, bot_id)).date():
        raise HTTPException(409, "A future appointment cannot be marked as visited yet")
    appointment.visited = visited
    appointment.updated_at = utcnow()
    return await save(db, appointment)


async def daily_register(db, bot_id, can_manage, day=None):
    today = now_local(await profile_for(db, bot_id)).date()
    day = day or today
    patients = await daily_patients(db, bot_id, day)
    return {
        "day": day,
        "today": today,
        "patients": [
            {
                **patient,
                "can_update_visit": (can_manage and patient["status"] == "booked" and day <= today),
            }
            for patient in patients
        ],
        "registered_count": sum(patient["status"] == "booked" for patient in patients),
        "visited_count": sum(patient["visited"] for patient in patients),
    }
