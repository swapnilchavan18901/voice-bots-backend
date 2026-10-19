from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MeetingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    organization_id: UUID
    title: str
    meet_url: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    agenda: str
    status: str
    auto_join: bool


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    recurring_event_id: str | None
    title: str | None
    meet_url: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    cancelled: bool
    meeting_id: UUID | None


class AssignMeeting(BaseModel):
    organization_id: UUID
    agenda: str = Field(default="", max_length=20000)
    auto_join: bool = True
    assign_series: bool = False


class MeetingSchedule(BaseModel):
    auto_join: bool
