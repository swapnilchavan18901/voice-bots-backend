from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.lower()


class Register(Credentials):
    name: str = Field(min_length=1, max_length=100)


class UserOut(ORMModel):
    id: UUID
    email: str
    name: str


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class OrgOut(ORMModel):
    id: UUID
    name: str


class MemberOut(ORMModel):
    user_id: UUID
    role: str


class InviteCreate(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"] = "member"

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.lower()


class InviteOut(BaseModel):
    id: UUID
    token: str
    expires_at: datetime


class InviteAccept(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    company_context: str = Field(default="", max_length=20000)
    language: Literal[
        "hi-IN",
        "en-IN",
        "bn-IN",
        "gu-IN",
        "kn-IN",
        "ml-IN",
        "mr-IN",
        "od-IN",
        "pa-IN",
        "ta-IN",
        "te-IN",
    ] = "hi-IN"


class BotCreate(BotConfig):
    template_id: UUID


class BotOut(BotCreate, ORMModel):
    id: UUID
    organization_id: UUID


class Offer(BaseModel):
    sdp: str = Field(min_length=1, max_length=100000)
    type: Literal["offer"] = "offer"


class Answer(BaseModel):
    sdp: str
    type: str
    pc_id: str


class TemplateOut(ORMModel):
    service_type: str
    id: UUID
    slug: str
    name: str
    description: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int = Field(default=90, ge=1, le=365)


class ApiKeyOut(ORMModel):
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str


class VoiceGrantOut(BaseModel):
    token: str
    offer_expires_at: datetime
    expires_at: datetime
    bot_id: UUID


class FeatureOut(BaseModel):
    bots: list[BotOut]
    templates: list[TemplateOut]


class WorkspaceOut(BaseModel):
    organization: OrgOut
    role: Literal["owner", "admin", "member"]
    can_manage: bool
    can_invite_admins: bool
    bots: list[BotOut]
    templates: list[TemplateOut]
    features: dict[Literal["clinic", "conversation"], FeatureOut]
    members: list[MemberOut]
