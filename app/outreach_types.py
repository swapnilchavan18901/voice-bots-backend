"""Audiva Outreach contracts. Phone numbers always include an explicit country code."""

import csv
import io
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.types import BotConfig, ORMModel

MAX_CONTACTS = 10000
MAX_CSV_BYTES = 2_000_000


class OutreachAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    sales_context: str = Field(min_length=1, max_length=20000)
    language: BotConfig.model_fields["language"].annotation = "en-IN"


class OutreachAgentOut(ORMModel, OutreachAgentConfig):
    id: UUID
    organization_id: UUID
    created_at: datetime


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    phone_number: str = Field(max_length=40)
    name: str = Field(default="", max_length=120)
    context: str = Field(default="", max_length=2000)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, value):
        value = re.sub(r"[\s().-]", "", value)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("Use E.164 format with country code, e.g. +919876543210")
        return value


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    agent_id: UUID
    mode: Literal["sequential", "parallel"] = "sequential"
    contacts: list[Contact | str] = Field(min_length=1, max_length=MAX_CONTACTS)

    @field_validator("contacts", mode="before")
    @classmethod
    def expand_numbers(cls, values):
        if isinstance(values, list):
            return [{"phone_number": v} if isinstance(v, str) else v for v in values]
        return values

    @field_validator("contacts")
    @classmethod
    def unique_numbers(cls, values):
        if len({v.phone_number for v in values}) != len(values):
            raise ValueError("Duplicate phone numbers are not allowed in a campaign")
        return values


class CampaignOut(ORMModel):
    id: UUID
    organization_id: UUID
    agent_id: UUID
    name: str
    mode: str
    status: str
    agent_snapshot: OutreachAgentConfig
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class OutreachCallOut(ORMModel):
    id: UUID
    position: int
    phone_number: str
    name: str
    status: str
    provider_call_sid: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


def parse_contacts_csv(content: bytes) -> list[Contact]:
    if len(content) > MAX_CSV_BYTES:
        raise ValueError("CSV must be at most 2 MB")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
        headers = reader.fieldnames or []
        if (
            "phone_number" not in headers
            or len(headers) != len(set(headers))
            or set(headers) - {"phone_number", "name", "context"}
        ):
            raise ValueError(
                "CSV headers: phone_number (required), name, context (optional)"
            )
        contacts = []
        for row in reader:
            if len(contacts) >= MAX_CONTACTS:
                raise ValueError(f"CSV supports at most {MAX_CONTACTS} contacts")
            try:
                contacts.append(Contact.model_validate(row))
            except ValueError:
                raise ValueError(
                    f"Invalid contact on CSV line {reader.line_num}"
                ) from None
        if not contacts:
            raise ValueError("CSV must contain at least one contact")
        return contacts
    except (UnicodeDecodeError, csv.Error):
        raise ValueError(
            "CSV must be valid UTF-8 with correctly quoted fields"
        ) from None
