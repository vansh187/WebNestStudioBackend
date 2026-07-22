import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectStatusUpsertRequest(BaseModel):
    project_name: str | None = None
    phase: str | None = None
    percent_complete: int | None = Field(default=None, ge=0, le=100)


class ProjectStatusResponse(BaseModel):
    id: uuid.UUID
    client_user_id: uuid.UUID
    project_name: str | None
    phase: str | None
    percent_complete: int | None
    updated_at: datetime

    model_config = {"from_attributes": True}
