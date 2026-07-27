import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr


class CollectedFields(BaseModel):
    project_type: str | None = None
    project_goal: str | None = None
    pages_features: list[str] | None = None
    budget_range: str | None = None
    timeline_expectation: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None


class PlanWeek(BaseModel):
    week_range: str
    title: str
    description: str


class GeneratePlanResponse(BaseModel):
    thread_id: uuid.UUID
    plan_id: uuid.UUID
    project_type: str
    total_weeks: int
    weeks: list[PlanWeek]
    html_summary: str
    pdf_download_url: str
    lead_id: uuid.UUID | None = None


class EmailPlanRequest(BaseModel):
    to_address: EmailStr | None = None


class EmailPlanResponse(BaseModel):
    sent: bool


class ChatbotLimitStatusResponse(BaseModel):
    remaining: int
    limit: int
    reset_at: datetime
