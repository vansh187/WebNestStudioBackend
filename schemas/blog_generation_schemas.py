import uuid
from datetime import datetime

from pydantic import BaseModel


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
