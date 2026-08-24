import uuid
from datetime import datetime

from pydantic import BaseModel

# Prompt/refinement length and emptiness are intentionally NOT enforced here via
# pydantic Field constraints. Those would raise FastAPI's generic 422 validation
# error before the request reaches GenerationService, which needs to own that
# check itself to return the spec's distinct 400 "invalid_prompt" status.


class GenerateRequest(BaseModel):
    prompt: str


class RefineRequest(BaseModel):
    refinement: str


class GenerateResponse(BaseModel):
    generation_id: uuid.UUID
    html: str
    provider_used: str


class HistoryListItem(BaseModel):
    generation_id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int


class HistoryListResponse(BaseModel):
    generations: list[HistoryListItem]


class GenerationMessageResponse(BaseModel):
    role: str
    content: str
    html_snapshot: str | None = None
    provider_used: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class HistoryDetailResponse(BaseModel):
    generation_id: uuid.UUID
    initial_prompt: str
    latest_html: str
    messages: list[GenerationMessageResponse]


class LimitStatusResponse(BaseModel):
    remaining: int
    limit: int
    reset_at: datetime
