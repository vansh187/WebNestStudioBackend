import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, model_validator

VALID_SOURCES = {"contact_form", "start_project", "newsletter", "resource_download", "consultation_booking", "chatbot"}


class LeadCreateRequest(BaseModel):
    source: str
    full_name: str | None = None
    email: EmailStr | None = None
    phone_number: str | None = None
    project_type: str | None = None
    budget_range: str | None = None
    timeline: str | None = None
    preferred_date: date | None = None
    preferred_time_slot: str | None = None
    resource_name: str | None = None
    message: str | None = None
    brief_file_url: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    referrer_url: str | None = None
    consent_given: bool = False

    @model_validator(mode="after")
    def check_source(self) -> "LeadCreateRequest":
        if self.source not in VALID_SOURCES:
            raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}")

        if self.source == "contact_form":
            missing = [
                field
                for field, value in (
                    ("full_name", self.full_name),
                    ("email", self.email),
                    ("phone_number", self.phone_number),
                    ("message", self.message),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"Missing required field(s) for contact_form: {', '.join(missing)}")
            if not self.consent_given:
                raise ValueError("consent_given must be true to submit the contact form")

        if self.source == "consultation_booking" and (not self.preferred_date or not self.preferred_time_slot):
            raise ValueError("preferred_date and preferred_time_slot are required for consultation_booking")

        if self.source == "resource_download" and not self.resource_name:
            raise ValueError("resource_name is required for resource_download")

        return self


class LeadResponse(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str | None
    phone_number: str | None
    source: str
    project_type: str | None
    budget_range: str | None
    timeline: str | None
    preferred_date: date | None
    preferred_time_slot: str | None
    resource_name: str | None
    message: str | None
    brief_file_url: str | None
    utm_source: str | None
    utm_medium: str | None
    utm_campaign: str | None
    referrer_url: str | None
    status: str
    consent_given: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LeadListResponse(BaseModel):
    total: int
    items: list[LeadResponse]


class LeadStatusUpdateRequest(BaseModel):
    status: str = Field(pattern="^(new|contacted|qualified|confirmed|completed|won|lost)$")
