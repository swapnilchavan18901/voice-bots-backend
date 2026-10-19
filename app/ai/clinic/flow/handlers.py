"""Reference Clinic Bot handlers adapted to async, bot-scoped database services."""

import asyncio
import logging
import re
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pipecat.frames.frames import TTSSpeakFrame
from pydantic import ValidationError

from app.ai.clinic.flow import nodes
from app.ai.clinic.flow.turns import confirmed
from app.business import clinic as svc
from app.db.models import ClinicDoctor
from app.schemas.clinic import Booking, ChangeAppointment, Reschedule

logger = logging.getLogger(__name__)
FILLERS = {
    "en-IN": "One moment, let me check that for you.",
    "hi-IN": "एक पल, मैं देख रही हूँ।",
    "mr-IN": "एक क्षण, मी तपासून सांगते.",
}


async def _call(fm, fn, *args):
    async def run():
        async with fm.state["session_factory"]() as db:
            await svc.require_clinic(db, fm.state["bot"].id)
            result = await fn(db, fm.state["bot"].id, *args)
            appointments = result if isinstance(result, list) else [result]
            for item in appointments:
                if isinstance(item, dict) and "doctor_id" in item and "patient_phone" in item:
                    doctor = await db.get(ClinicDoctor, item["doctor_id"])
                    item["doctor_name"] = doctor.name if doctor else "Doctor record unavailable"
            return result

    task = asyncio.create_task(run())
    done, _ = await asyncio.wait({task}, timeout=0.7)
    if not done:
        await fm.worker.queue_frame(
            TTSSpeakFrame(
                text=FILLERS.get(fm.state["bot"].language, FILLERS["en-IN"]),
                append_to_context=False,
            )
        )
    try:
        return await task, None
    except HTTPException as exc:
        logger.info(
            "Clinic action rejected: action=%s status=%s detail=%s",
            fn.__name__,
            exc.status_code,
            exc.detail,
        )
        return None, exc.detail
    except (ValueError, TypeError, ValidationError) as exc:
        return None, f"Invalid input: {exc}"
    except Exception:
        logger.exception("Clinic action failed: %s", fn.__name__)
        return None, "Booking service unavailable. Please try again."


def _summary(appointment):
    return {
        "appointment_id": str(appointment["id"]),
        "doctor": appointment["doctor_name"],
        "time": appointment["start_time"].isoformat(timespec="minutes"),
        "reason": appointment["reason"],
    }


def _confirmation_required(fm):
    return {
        "ok": False,
        "error": "Ask for confirmation and wait for the caller to clearly say yes.",
    }, None


async def save_caller(args, fm):
    digits = re.sub(r"\D", "", args["phone"])
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        return {
            "ok": False,
            "error": "Too many digits." if len(digits) > 10 else "Incomplete phone number.",
            "digits_heard": " ".join(digits),
            "digits_missing": max(0, 10 - len(digits)),
        }, None
    if not args["name"].strip():
        return {"ok": False, "error": "Ask for the caller's name."}, None
    fm.state.update(name=args["name"].strip(), phone=digits)
    upcoming, error = await _call(fm, svc.upcoming, digits)
    fm.state["upcoming"] = [_summary(a) for a in upcoming or []]
    return {
        "ok": True,
        "upcoming_appointments": fm.state["upcoming"],
        "lookup_error": error,
    }, nodes.intent()


async def choose_book(args, fm):
    fm.state["mode"] = "book"
    return {"ok": True}, nodes.collect_slot(fm.state)


async def choose_reschedule(args, fm):
    if not fm.state.get("upcoming"):
        return {"ok": False, "error": "This phone number has no upcoming appointments."}, None
    fm.state["mode"] = "reschedule"
    return {"ok": True}, nodes.pick_appointment(fm.state)


async def choose_cancel(args, fm):
    if not fm.state.get("upcoming"):
        return {"ok": False, "error": "This phone number has no upcoming appointments."}, None
    fm.state["mode"] = "cancel"
    return {"ok": True}, nodes.pick_appointment(fm.state)


async def ask_question(args, fm):
    return {"ok": True}, nodes.faq(fm.state)


async def unanswered_question(args, fm):
    fm.state.setdefault("unanswered", []).append(args.get("question", ""))
    logger.info("Clinic FAQ gap: bot_id=%s", fm.state["bot"].id)
    return {"ok": True, "answer": None, "front_desk_phone": fm.state["facts"].get("phone")}, None


async def clinic_info(args, fm):
    topic = args.get("topic")
    facts = fm.state["facts"]
    return {
        "ok": True,
        "answer": facts.get(topic),
        "context": facts.get("company_context", ""),
        "instruction": "Use only these clinic facts; if the answer is absent, say it is unknown.",
    }, None


async def select_appointment(args, fm):
    match = next(
        (
            a
            for a in fm.state.get("upcoming", [])
            if a["appointment_id"] == str(args["appointment_id"])
        ),
        None,
    )
    if not match:
        return {"ok": False, "error": "That appointment isn't one of this caller's bookings."}, None
    fm.state["appointment"] = match
    if fm.state["mode"] == "cancel":
        fm.state["confirmation_turn"] = fm.state.get("caller_turn", 0)
        return {"ok": True}, nodes.confirm_cancel(fm.state)
    return {"ok": True}, nodes.collect_slot(fm.state)


async def check_availability(args, fm):
    try:
        day = date.fromisoformat(args["date"])
    except (ValueError, TypeError):
        return {"ok": False, "error": "Date must be YYYY-MM-DD."}, None
    data, error = await _call(fm, svc.availability, day, args.get("doctor_id"))
    return ({"ok": False, "error": error} if error else {"ok": True, "data": data}), None


async def select_slot(args, fm):
    try:
        start = datetime.fromisoformat(args["start_time"])
        if start.tzinfo:
            start = start.astimezone(
                ZoneInfo(fm.state["facts"].get("timezone", "Asia/Kolkata"))
            ).replace(tzinfo=None)
        doctor_id = int(args["doctor_id"])
    except (ValueError, TypeError):
        return {"ok": False, "error": "Provide a doctor ID and YYYY-MM-DDTHH:MM slot."}, None
    available, error = await _call(fm, svc.availability, start.date(), doctor_id)
    if error:
        return {"ok": False, "error": error}, None
    doctor = available["doctors"][0]
    if start not in [datetime.fromisoformat(t) for t in doctor["slots"]]:
        return {
            "ok": False,
            "error": "That slot is not available.",
            "free_slots": doctor["slots"],
        }, None
    fm.state["slot"] = {
        "doctor_id": doctor_id,
        "doctor": doctor["name"],
        "start_time": start.isoformat(timespec="minutes"),
        "reason": args.get("reason"),
    }
    fm.state["confirmation_turn"] = fm.state.get("caller_turn", 0)
    node = (
        nodes.confirm_reschedule(fm.state)
        if fm.state["mode"] == "reschedule"
        else nodes.confirm_booking(fm.state)
    )
    return {"ok": True}, node


async def confirm_booking(args, fm):
    if fm.current_node != "confirm_booking" or not confirmed(fm.state):
        return _confirmation_required(fm)
    fm.state["confirmation_turn"] = fm.state["caller_turn"]
    state = fm.state
    appointment, error = await _call(
        fm,
        svc.book,
        Booking(
            doctor_id=state["slot"]["doctor_id"],
            patient_name=state["name"],
            patient_phone=state["phone"],
            start_time=state["slot"]["start_time"],
            reason=state["slot"]["reason"],
        ),
    )
    if error:
        return {"ok": False, "error": error}, nodes.collect_slot(state)
    state["outcome"] = (
        f"Booked with {appointment['doctor_name']} at {appointment['start_time'].isoformat()}."
    )
    return {"ok": True, "appointment": _summary(appointment)}, nodes.close(state)


async def confirm_reschedule(args, fm):
    if fm.current_node != "confirm_reschedule" or not confirmed(fm.state):
        return _confirmation_required(fm)
    fm.state["confirmation_turn"] = fm.state["caller_turn"]
    state = fm.state
    appointment, error = await _call(
        fm,
        svc.change,
        Reschedule(
            appointment_id=UUID(state["appointment"]["appointment_id"]),
            patient_phone=state["phone"],
            doctor_id=state["slot"]["doctor_id"],
            new_start_time=state["slot"]["start_time"],
        ),
    )
    if error:
        return {"ok": False, "error": error}, nodes.collect_slot(state)
    state["outcome"] = (
        f"Moved appointment to {appointment['start_time'].isoformat()} with "
        f"{appointment['doctor_name']}."
    )
    return {"ok": True, "appointment": _summary(appointment)}, nodes.close(state)


async def confirm_cancel(args, fm):
    if fm.current_node != "confirm_cancel" or not confirmed(fm.state):
        return _confirmation_required(fm)
    fm.state["confirmation_turn"] = fm.state["caller_turn"]
    state = fm.state

    async def cancel(db, bot_id, data):
        return await svc.change(db, bot_id, data, cancel=True)

    appointment, error = await _call(
        fm,
        cancel,
        ChangeAppointment(
            appointment_id=UUID(state["appointment"]["appointment_id"]),
            patient_phone=state["phone"],
        ),
    )
    if error:
        return {"ok": False, "error": error}, nodes.intent()
    state["outcome"] = (
        f"Cancelled appointment with {appointment['doctor_name']} at "
        f"{appointment['start_time'].isoformat()}."
    )
    state["upcoming"] = [
        a for a in state["upcoming"] if a["appointment_id"] != str(appointment["id"])
    ]
    return {"ok": True}, nodes.close(state)


async def change_slot(args, fm):
    fm.state.pop("slot", None)
    return {"ok": True}, nodes.collect_slot(fm.state)


async def keep_appointment(args, fm):
    fm.state["outcome"] = "Caller decided to keep the appointment."
    return {"ok": True}, nodes.close(fm.state)


async def another_request(args, fm):
    for key in ("mode", "slot", "appointment", "outcome"):
        fm.state.pop(key, None)
    upcoming, error = await _call(fm, svc.upcoming, fm.state["phone"])
    fm.state["upcoming"] = [_summary(a) for a in upcoming or []]
    return {
        "ok": True,
        "upcoming_appointments": fm.state["upcoming"],
        "lookup_error": error,
    }, nodes.intent()


async def end_call(args, fm):
    return {"ok": True}, nodes.goodbye()


async def emergency(args, fm):
    logger.warning("Clinic emergency reported: bot_id=%s", fm.state["bot"].id)
    return {"ok": True}, nodes.emergency()


async def transfer_to_human(args, fm):
    fm.state["handoff_reason"] = args.get("reason", "")
    fm.state["confirmation_turn"] = fm.state.get("caller_turn", 0)
    return {"ok": True}, nodes.confirm_transfer(fm.state)


async def confirm_transfer(args, fm):
    if fm.current_node != "confirm_transfer" or not confirmed(fm.state):
        return _confirmation_required(fm)
    fm.state["confirmation_turn"] = fm.state["caller_turn"]
    state = fm.state
    state["handoff"] = {
        "bot_id": str(state["bot"].id),
        "reason": state.get("handoff_reason"),
        "caller": state.get("name"),
        "phone": state.get("phone"),
        "was_doing": state.get("mode"),
        "slot_in_progress": state.get("slot"),
        "unanswered_questions": state.get("unanswered", []),
    }
    logger.warning("HANDOFF REQUEST LOGGED: %s", state["handoff"])
    return {"ok": True, "request_logged": True, "staff_notified": False}, nodes.transfer(state)


async def decline_transfer(args, fm):
    fm.state["outcome"] = "Caller declined to log a staff request."
    return {"ok": True}, nodes.close(fm.state)
