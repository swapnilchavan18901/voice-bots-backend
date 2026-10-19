"""AI layer — the conversation map. One function per node.

greet_identify → intent ─┬─ book ───────► collect_slot ─► confirm_booking ────► close ─► goodbye
                         ├─ reschedule ─► pick_appointment ─► collect_slot ─► confirm_reschedule ─►
                         ├─ cancel ─────► pick_appointment ─► confirm_cancel ──► close
                         └─ question ───► faq ─┬─ book / reschedule / cancel ─► (back into the flow)
                                               └─ done ─► goodbye
global (any node): report_emergency  ─► emergency ─► end
                   transfer_to_human ─► transfer  ─► end (handoff summary logged for staff)
                   clinic_info       ─► answers a quick fact, stays on the same node
"""

from pipecat.flows import FlowsFunctionSchema, NodeConfig

from app.ai.clinic.flow import handlers as h
from app.ai.clinic.flow.prompts import doctors_text, facts_text, role_message
from app.constants.constants import TIME, TOPICS


def _fn(
    name: str,
    description: str,
    handler,
    properties: dict | None = None,
    required: list | None = None,
):
    return FlowsFunctionSchema(
        name=name,
        description=description,
        properties=properties or {},
        required=required or [],
        handler=handler,
    )


def _task(text: str) -> list[dict]:
    return [{"role": "developer", "content": text.replace("{{", r"\{{")}]


def _appt_line(a: dict) -> str:
    return f"appointment {a['appointment_id']}: {a['doctor']} at {a['time']}"


# ---------- nodes ----------


def greet_identify(state) -> NodeConfig:
    return {
        "name": "greet_identify",
        "role_message": role_message(state),
        "task_messages": _task(
            "Greet the caller warmly as the clinic receptionist and ask for their name. "
            "Once you have the name, ask for their ten digit mobile number and tell them"
            " they can say it slowly. "
            "Ask for one thing at a time. "
            "Callers often pause while saying the number. If you only got part of it, do"
            " NOT ask for the whole "
            "number again: repeat the digits you have, one by one, and ask for the rest. "
            "Join the parts together. When you have the name and all ten digits, call save_caller."
        ),
        "functions": [
            _fn(
                "save_caller",
                "Save the caller's name and phone number.",
                h.save_caller,
                {
                    "name": {"type": "string"},
                    "phone": {"type": "string", "description": "Digits only"},
                },
                ["name", "phone"],
            )
        ],
    }


def intent() -> NodeConfig:
    return {
        "name": "intent",
        "task_messages": _task(
            "Thank the caller by name. If they have upcoming appointments, mention them "
            "in one short sentence. "
            "Ask whether they want to book a new appointment, reschedule, or cancel. "
            "Call the matching function. If they have questions (hours, address, fees, "
            "parking, doctors), "
            "call ask_question."
        ),
        "functions": [
            _fn("choose_book", "Caller wants a new appointment.", h.choose_book),
            _fn(
                "choose_reschedule",
                "Caller wants to move an existing appointment.",
                h.choose_reschedule,
            ),
            _fn("choose_cancel", "Caller wants to cancel an appointment.", h.choose_cancel),
            _fn("ask_question", "Caller has general questions about the clinic.", h.ask_question),
        ],
    }


def faq(state) -> NodeConfig:
    """Free-form Q&A from clinic context, with functions to return to the workflow."""
    return {
        "name": "faq",
        "task_messages": _task(
            "Answer the caller's questions about the clinic using ONLY these facts:\n"
            f"{facts_text(state)}\n"
            "Keep each answer to one or two short sentences, then ask if they have "
            "another question "
            "or would like to book. Never guess. If the answer is not in the facts, call "
            "unanswered_question and give them the front desk number.\n"
            "When they want to book, reschedule or cancel, call the matching function. "
            "If they have no more questions, call end_call."
        ),
        "functions": [
            _fn(
                "unanswered_question",
                "The caller asked something the facts don't cover.",
                h.unanswered_question,
                {"question": {"type": "string"}},
                ["question"],
            ),
            # fallback back into the flow — the same handlers the intent node uses
            _fn("choose_book", "Caller now wants a new appointment.", h.choose_book),
            _fn(
                "choose_reschedule", "Caller now wants to move an appointment.", h.choose_reschedule
            ),
            _fn("choose_cancel", "Caller now wants to cancel an appointment.", h.choose_cancel),
            _fn("end_call", "Caller has no more questions.", h.end_call),
        ],
    }


def pick_appointment(state: dict) -> NodeConfig:
    appts = "; ".join(_appt_line(a) for a in state["upcoming"])
    action = "cancel" if state["mode"] == "cancel" else "reschedule"
    task = (
        f"The caller wants to {action}. Their upcoming bookings: {appts}. "
        "If there is only one, confirm it's that one. If several, ask which. "
        "Then call select_appointment with its id."
    )
    return {
        "name": "pick_appointment",
        "task_messages": _task(task),
        "functions": [
            _fn(
                "select_appointment",
                "Select which existing appointment the caller means.",
                h.select_appointment,
                {"appointment_id": {"type": "string", "format": "uuid"}},
                ["appointment_id"],
            )
        ],
    }


def collect_slot(state: dict) -> NodeConfig:
    if state.get("mode") == "reschedule":
        a = state["appointment"]
        goal = (
            f"The caller is moving {_appt_line(a)}. Ask for the new date and time "
            "(same doctor unless they ask to change)."
        )
    else:
        goal = (
            "Introduce the available doctors and their specialties briefly, then ask which doctor "
            "or what problem the caller wants help with. Ask one question at a time. "
            "If the caller already described a symptom, do not repeat the whole specialist list. "
            "For a fever, offer the general physician for an adult or the pediatrician for a child "
            "only if those specialties exist in this clinic's doctor list; ask whether the patient "
            "is an adult or child when needed. Do not suggest a dermatologist for fever alone. "
            "Confirm the relevant doctor, then ask for the preferred date and time. "
            "Also ask briefly for the reason for the visit if not already known."
        )
    return {
        "name": "collect_slot",
        "task_messages": _task(
            f"{goal} Doctors: {doctors_text(state)}. "
            "Call check_availability to see free slots, offer two or three close to what"
            " they want, "
            "and when they pick one call select_slot. Never invent slots. "
            "Do not treat example dates in clinic background as verified live availability. "
            "An ok=false result is not an outage unless the error says service unavailable; "
            "explain missing configuration or invalid input accurately. "
            "If a requested time is absent, offer the verified free slots and wait for a choice."
        ),
        "functions": [
            _fn(
                "check_availability",
                "List free slots on a date, for one doctor or all doctors.",
                h.check_availability,
                {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "doctor_id": {"type": "integer"},
                },
                ["date"],
            ),
            _fn(
                "select_slot",
                "The caller has chosen a doctor and time.",
                h.select_slot,
                {
                    "doctor_id": {"type": "integer"},
                    "start_time": TIME,
                    "reason": {"type": "string", "description": "Short reason for the visit"},
                },
                ["doctor_id", "start_time"],
            ),
        ],
    }


def confirm_booking(state: dict) -> NodeConfig:
    s = state["slot"]
    return {
        "name": "confirm_booking",
        "task_messages": _task(
            f"Read back: {state['name']}, phone {state['phone']}, with {s['doctor']} at "
            f"{s['start_time']}"
            f"{', for ' + s['reason'] if s.get('reason') else ''}. Ask them to confirm. "
            "If yes call confirm_booking. If they want a different time or doctor call change_slot."
        ),
        "functions": [
            _fn("confirm_booking", "Caller said yes. Book it.", h.confirm_booking),
            _fn("change_slot", "Caller wants a different time or doctor.", h.change_slot),
        ],
    }


def confirm_reschedule(state: dict) -> NodeConfig:
    a, s = state["appointment"], state["slot"]
    return {
        "name": "confirm_reschedule",
        "task_messages": _task(
            f"Read back: move {_appt_line(a)} to {s['start_time']} with {s['doctor']}. "
            f"Ask them to confirm. "
            "If yes call confirm_reschedule. If they want another time call change_slot."
        ),
        "functions": [
            _fn("confirm_reschedule", "Caller said yes. Move it.", h.confirm_reschedule),
            _fn("change_slot", "Caller wants a different time.", h.change_slot),
        ],
    }


def confirm_cancel(state: dict) -> NodeConfig:
    return {
        "name": "confirm_cancel",
        "task_messages": _task(
            f"Ask the caller to clearly confirm cancelling {_appt_line(state['appointment'])}. "
            "Only on a clear yes call confirm_cancel. If they change their mind call "
            "keep_appointment."
        ),
        "functions": [
            _fn("confirm_cancel", "Caller clearly said yes. Cancel it.", h.confirm_cancel),
            _fn("keep_appointment", "Caller does not want to cancel.", h.keep_appointment),
        ],
    }


def close(state: dict) -> NodeConfig:
    outcome = state.get("outcome", "")
    return {
        "name": "close",
        "task_messages": _task(
            f"What happened: {outcome} Tell the caller in one short sentence, "
            "then ask if there is anything else. If yes call another_request, if no call end_call."
        ),
        "functions": [
            _fn("another_request", "Caller needs something else.", h.another_request),
            _fn("end_call", "Caller is done.", h.end_call),
        ],
    }


def goodbye() -> NodeConfig:
    return {
        "name": "goodbye",
        "task_messages": _task("Say a short, warm goodbye."),
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }


def emergency() -> NodeConfig:
    return {
        "name": "emergency",
        "task_messages": _task(
            "This may be an emergency. Calmly tell the caller to contact local emergency"
            " services or go to the nearest hospital "
            "emergency room right now. Keep it to two short sentences."
        ),
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }


def transfer(state: dict) -> NodeConfig:
    """Clean exit to a human: tell the caller what happens next, then end the call."""
    front_desk = state["facts"].get("phone")
    contact = f"They can call {front_desk}." if front_desk else "No front desk number was provided."
    return {
        "name": "transfer",
        "task_messages": _task(
            "The caller's request has been logged in the booking system. Say that briefly. "
            "No staff notification, live transfer or callback delivery is connected; do not claim "
            f"staff were notified or promise a callback. {contact} Do not ask any more questions."
        ),
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }


def global_functions() -> list:
    """Functions available at every node."""
    return [
        _fn(
            "report_emergency",
            "Caller describes a medical emergency (chest pain, heavy bleeding, breathing"
            " trouble, unconscious person, etc.).",
            h.emergency,
            {"description": {"type": "string"}},
            ["description"],
        ),
        _fn(
            "transfer_to_human",
            "Caller asks for a real person or staff member, is upset, or you cannot help"
            " them after two tries.",
            h.transfer_to_human,
            {"reason": {"type": "string", "description": "Why they need a person, in a few words"}},
            ["reason"],
        ),
        _fn(
            "clinic_info",
            "Look up a quick clinic fact when the caller asks in the middle of something else. "
            "Answer briefly, then continue the current step.",
            h.clinic_info,
            {"topic": {"type": "string", "enum": TOPICS}},
            ["topic"],
        ),
    ]


def confirm_transfer(state) -> NodeConfig:
    return {
        "name": "confirm_transfer",
        "task_messages": _task(
            "Ask if the caller wants their request logged for staff. Explain that automatic "
            "staff notification and callback delivery are not connected. Ask for a clear yes, "
            "then WAIT for the next caller turn. If yes call confirm_transfer; otherwise"
            " decline_transfer."
        ),
        "functions": [
            _fn(
                "confirm_transfer",
                "Caller clearly agreed to log the staff request.",
                h.confirm_transfer,
            ),
            _fn("decline_transfer", "Caller does not want a staff request.", h.decline_transfer),
        ],
    }
