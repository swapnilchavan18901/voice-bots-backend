import re
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClinicInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Caller(ClinicInput):
    patient_phone: str

    @field_validator("patient_phone")
    @classmethod
    def phone(cls, value):
        digits = re.sub(r"[\s()+-]", "", value)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if not re.fullmatch(r"[0-9]{10}", digits):
            raise ValueError("Provide a ten-digit Indian phone number")
        return digits


class Booking(Caller):
    doctor_id: int = Field(gt=0)
    patient_name: str = Field(min_length=1, max_length=120)
    start_time: datetime
    reason: str | None = Field(default=None, max_length=2000)


class ChangeAppointment(Caller):
    appointment_id: UUID


class Reschedule(ChangeAppointment):
    new_start_time: datetime
    doctor_id: int | None = Field(default=None, gt=0)


class VisitUpdate(ClinicInput):
    visited: bool


class PatientOut(BaseModel):
    id: UUID
    doctor_id: int
    doctor_name: str
    patient_name: str
    patient_phone: str
    start_time: datetime
    reason: str | None
    status: str
    visited: bool
    can_update_visit: bool


class RegisterOut(BaseModel):
    day: date
    today: date
    patients: list[PatientOut]
    registered_count: int
    visited_count: int
