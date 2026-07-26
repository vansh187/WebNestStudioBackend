import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class BlogGenerationTriggerRequest(BaseModel):
    # Optional - when omitted, the pipeline picks its own topic and enforces
    # historical topic-uniqueness as usual. When provided, the model is
    # directed to write on this exact subject and the uniqueness check is
    # skipped for this run (the caller asked for this topic deliberately).
    topic_hint: str | None = Field(default=None, min_length=1, max_length=500)


class BlogGenerationLogResponse(BaseModel):
    id: uuid.UUID
    attempted_at: datetime
    success: bool
    llm_used: str | None
    topic_tag: str | None
    blog_post_id: uuid.UUID | None
    error_message: str | None
    trigger_source: str

    model_config = {"from_attributes": True}


class BlogGenerationTriggerResponse(BaseModel):
    success: bool
    detail: str
    post_id: uuid.UUID | None = None
    slug: str | None = None
