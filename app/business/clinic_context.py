"""Clinic facts and scheduling configuration stored inside each bot's company_context."""

import json
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from app.db.models import ClinicDoctor


class ContextDoctor(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    specialty: str = Field(min_length=1, max_length=80)
    available_slots: list[datetime] | None = Field(default=None, max_length=2000)

    @field_validator("available_slots")
    @classmethod
    def local_slots(cls, value):
        if value is not None:
            if any(t.tzinfo or t.second or t.microsecond for t in value):
                raise ValueError("Published slots must use minute-precision clinic-local datetimes")
            return sorted(set(value))
        return value


class ClinicDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=100)
    fees: str | None = Field(default=None, max_length=2000)
    parking: str | None = Field(default=None, max_length=2000)
    booking: str | None = Field(default=None, max_length=2000)
    timezone: str | None = None
    working_days: list[int] | None = None
    open_time: time | None = None
    close_time: time | None = None
    lunch_start: time | None = None
    lunch_end: time | None = None
    slot_minutes: int | None = Field(default=None, ge=5, le=240)
    doctors: list[ContextDoctor] = Field(default_factory=list, max_length=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Provide a valid IANA timezone") from None
        return value

    @field_validator("working_days")
    @classmethod
    def valid_days(cls, value):
        if value is not None and (not value or any(day < 0 or day > 6 for day in value)):
            raise ValueError("Working days must contain weekdays from 0 (Monday) to 6 (Sunday)")
        return sorted(set(value)) if value is not None else None

    @field_validator("open_time", "close_time", "lunch_start", "lunch_end")
    @classmethod
    def minute_precision(cls, value):
        if value and (value.second or value.microsecond or value.tzinfo is not None):
            raise ValueError("Use local HH:MM times without seconds or timezone offsets")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.open_time and self.close_time and self.open_time >= self.close_time:
            raise ValueError("Closing time must follow opening time")
        if bool(self.lunch_start) != bool(self.lunch_end):
            raise ValueError("Provide both lunch times or neither")
        if self.lunch_start and self.lunch_end:
            if self.lunch_start >= self.lunch_end:
                raise ValueError("Lunch end must follow lunch start")
            if self.open_time and self.lunch_start < self.open_time:
                raise ValueError("Lunch must fall within opening hours")
            if self.close_time and self.lunch_end > self.close_time:
                raise ValueError("Lunch must fall within opening hours")
        if len({d.name.casefold() for d in self.doctors}) != len(self.doctors):
            raise ValueError("Doctor names must be unique within a bot")
        return self

    def calendar(self):
        if (
            self.timezone
            and self.slot_minutes
            and self.doctors
            and any(d.available_slots is not None for d in self.doctors)
        ):
            return self
        required = (
            self.timezone,
            self.working_days,
            self.open_time,
            self.close_time,
            self.slot_minutes,
        )
        if any(value is None or value == "" for value in required):
            raise HTTPException(
                422,
                "Configure the clinic timezone, working days, opening/closing "
                "times and slot duration in the agent context before booking",
            )
        return self


def _clean(value):
    return re.sub(r"[*`_]", "", value).strip()


def _clock(value):
    value = value.strip().upper()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    raise ValueError("Unsupported time")


def markdown_details(context):
    """Read labelled clinic facts and dated doctor-slot tables without inventing values.

    Unrecognised free text remains conversation background; it cannot become a calendar.
    Published slots are an allowlist, not examples of a recurring schedule.
    """
    if not isinstance(context, str):
        return ClinicDetails()
    facts, doctors, published = {}, {}, {}
    section, day, has_published = "facts", None, False
    for raw in context.splitlines():
        line = _clean(raw.strip())
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            title = heading[1]
            if title.lower() in {"clinic details", "clinic info", "clinic information"}:
                section = "facts"
            elif title.lower() == "doctors":
                section = "doctors"
            elif "available appointment slots" in title.lower():
                section, has_published = "slots", True
            elif section == "slots" and raw.lstrip().startswith("###"):
                for fmt in ("%A, %d %B %Y", "%d %B %Y", "%Y-%m-%d"):
                    try:
                        day = datetime.strptime(title, fmt).date()
                        break
                    except ValueError:
                        day = None
            else:
                section, day = "ignored", None
                if raw.startswith("# "):
                    facts["name"] = title
                    section = "facts"
            continue
        fact = re.match(r"^[-*]\s*([^:]+):\s*(.+)$", line)
        if fact:
            if section != "facts":
                continue
            key, value = fact[1].strip().lower(), fact[2].strip()
            mapped = {
                "name": "name",
                "address": "address",
                "phone": "phone",
                "front desk phone": "phone",
                "fees": "fees",
                "parking": "parking",
                "booking policy": "booking",
                "cancellation policy": "booking",
                "timezone": "timezone",
            }.get(key)
            if mapped:
                facts[mapped] = value
            if key == "appointment length":
                match = re.fullmatch(r"(\d+)\s*minutes?", value, re.I)
                if match:
                    facts["slot_minutes"] = int(match[1])
            if key == "hours":
                clocks = re.findall(r"\d{1,2}(?::\d{2})?\s*(?:AM|PM)", value, re.I)
                if len(clocks) in (2, 4):
                    facts["open_time"], facts["close_time"] = _clock(clocks[0]), _clock(clocks[-1])
                    if len(clocks) == 4:
                        facts["lunch_start"], facts["lunch_end"] = (
                            _clock(clocks[1]),
                            _clock(clocks[2]),
                        )
                weekdays = [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                ]
                days = [i for i, name in enumerate(weekdays) if name in value.lower()]
                if len(days) == 2 and re.search(r"[–—-]|\bto\b", value, re.I):
                    days = list(range(days[0], days[1] + 1))
                if days:
                    facts["working_days"] = days
            continue
        if not line.startswith("|"):
            continue
        cells = [_clean(c) for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0].lower() == "doctor" or set(cells[0]) <= {"-", ":", " "}:
            continue
        if section == "doctors":
            doctors[cells[0]] = {"name": cells[0], "specialty": cells[1]}
            if len(cells) > 2 and cells[2]:
                fee = f"{cells[0]}: {cells[2]}"
                facts["fees"] = (facts.get("fees", "") + "; " + fee).strip("; ")
        elif section == "slots" and day:
            times = []
            for item in cells[1].split(","):
                try:
                    times.append(datetime.combine(day, _clock(item)))
                except ValueError:
                    # Ambiguous/malformed times are not advertised as bookable slots.
                    continue
            published.setdefault(cells[0], []).extend(times)
    if has_published:
        for name, doc in doctors.items():
            doc["available_slots"] = sorted(set(published.get(name, [])))
    # This voice platform uses India-local times when a context supplies no timezone.
    # Explicit timezone in the context always takes precedence.
    if "timezone" not in facts and (has_published or "open_time" in facts):
        facts["timezone"] = "Asia/Kolkata"
    facts["doctors"] = list(doctors.values())
    try:
        return ClinicDetails.model_validate(facts)
    except (ValidationError, ValueError):
        return ClinicDetails()


def details(context):
    try:
        data = json.loads(context)
    except (json.JSONDecodeError, TypeError):
        return markdown_details(context)
    if not isinstance(data, dict) or "clinic" not in data:
        return ClinicDetails()
    try:
        base = markdown_details(data.get("background", "")).model_dump(exclude_none=True)
        overrides = data["clinic"]
        if not isinstance(overrides, dict):
            raise ValueError("clinic must be an object")
        if "doctors" in overrides and isinstance(overrides["doctors"], list):
            previous = {d["name"]: d for d in base["doctors"]}
            overrides = {
                **overrides,
                "doctors": [
                    {**previous.get(d.get("name"), {}), **d} if isinstance(d, dict) else d
                    for d in overrides["doctors"]
                ],
            }
        return ClinicDetails.model_validate({**base, **overrides})
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, "Invalid clinic context: " + str(exc)) from None


def background(context):
    try:
        data = json.loads(context)
        if isinstance(data, dict) and "clinic" in data:
            value = data.get("background", "")
            return value if isinstance(value, str) else json.dumps(value)
    except (json.JSONDecodeError, TypeError):
        pass
    return context


async def sync_doctors(db, bot):
    profile = details(bot.company_context)
    existing = {
        d.name: d
        for d in (await db.scalars(select(ClinicDoctor).where(ClinicDoctor.bot_id == bot.id))).all()
    }
    for doc in profile.doctors:
        if doc.name in existing:
            existing[doc.name].specialty = doc.specialty
        else:
            db.add(ClinicDoctor(bot_id=bot.id, name=doc.name, specialty=doc.specialty))
    # Removed doctors remain for historical appointment references, but are not bookable.
    await db.flush()
