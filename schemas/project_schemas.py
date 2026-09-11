import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from core.constants import SDLC_STAGE_KEYS

STATUS_VALUES = ("active", "on_hold", "completed", "archived")


class ProjectStageResponse(BaseModel):
    key: str
    label: str
    order_index: int
    state: str
    note: str | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectSummaryResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    current_stage: str
    current_stage_label: str
    progress_percent: int
    conversation_id: uuid.UUID | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectDetailResponse(ProjectSummaryResponse):
    summary: str | None
    created_at: datetime
    stages: list[ProjectStageResponse]


class AdminProjectRow(ProjectSummaryResponse):
    client_email: str
    client_name: str | None


class AdminProjectListResponse(BaseModel):
    projects: list[AdminProjectRow]
    total: int


class MyProjectListResponse(BaseModel):
    projects: list[ProjectSummaryResponse]


class AdminProjectCreateRequest(BaseModel):
    client_email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=2000)
    current_stage: str = "requirements"
    create_conversation: bool = True

    @field_validator("current_stage")
    @classmethod
    def _valid_stage(cls, value: str) -> str:
        if value not in SDLC_STAGE_KEYS:
            raise ValueError("current_stage is not a valid SDLC stage")
        return value


class AdminProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=2000)
    status: str | None = None
    current_stage: str | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    create_conversation: bool | None = None

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str | None) -> str | None:
        if value is not None and value not in STATUS_VALUES:
            raise ValueError("status must be one of: " + ", ".join(STATUS_VALUES))
        return value

    @field_validator("current_stage")
    @classmethod
    def _valid_stage(cls, value: str | None) -> str | None:
        if value is not None and value not in SDLC_STAGE_KEYS:
            raise ValueError("current_stage is not a valid SDLC stage")
        return value


class AdminStageUpdateRequest(BaseModel):
    state: str | None = None
    note: str | None = None

    @field_validator("state")
    @classmethod
    def _valid_state(cls, value: str | None) -> str | None:
        if value is not None and value not in ("pending", "in_progress", "done"):
            raise ValueError("state must be pending, in_progress or done")
        return value
