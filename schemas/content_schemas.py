import uuid
from datetime import datetime

from pydantic import BaseModel, Field

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


def slug_field(required: bool = True):
    if required:
        return Field(pattern=SLUG_PATTERN, min_length=1, max_length=200)
    return Field(default=None, pattern=SLUG_PATTERN, min_length=1, max_length=200)


class ServiceCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    slug: str = slug_field()
    short_description: str | None = None
    full_description: str | None = None
    icon_url: str | None = None
    tech_tags: list[str] | None = None
    display_order: int | None = None
    is_published: bool = True


class ServiceUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    slug: str | None = slug_field(required=False)
    short_description: str | None = None
    full_description: str | None = None
    icon_url: str | None = None
    tech_tags: list[str] | None = None
    display_order: int | None = None
    is_published: bool | None = None


class ServiceResponse(BaseModel):
    id: uuid.UUID
    title: str | None
    slug: str
    short_description: str | None
    full_description: str | None
    icon_url: str | None
    tech_tags: list[str] | None
    display_order: int | None
    is_published: bool

    model_config = {"from_attributes": True}


class PortfolioCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    slug: str = slug_field()
    category: str | None = None
    short_description: str | None = None
    full_description: str | None = None
    tech_stack: list[str] | None = None
    cover_image_url: str | None = None
    gallery_urls: list[str] | None = None
    result_metrics: str | None = None
    client_name: str | None = None
    display_order: int | None = None
    is_published: bool = True


class PortfolioUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    slug: str | None = slug_field(required=False)
    category: str | None = None
    short_description: str | None = None
    full_description: str | None = None
    tech_stack: list[str] | None = None
    cover_image_url: str | None = None
    gallery_urls: list[str] | None = None
    result_metrics: str | None = None
    client_name: str | None = None
    display_order: int | None = None
    is_published: bool | None = None


class PortfolioResponse(BaseModel):
    id: uuid.UUID
    title: str | None
    slug: str
    category: str | None
    short_description: str | None
    full_description: str | None
    tech_stack: list[str] | None
    cover_image_url: str | None
    gallery_urls: list[str] | None
    result_metrics: str | None
    client_name: str | None
    display_order: int | None
    is_published: bool

    model_config = {"from_attributes": True}


class TestimonialCreateRequest(BaseModel):
    client_name: str = Field(min_length=1)
    company: str | None = None
    quote: str = Field(min_length=1)
    rating: int = Field(ge=1, le=5)
    avatar_url: str | None = None
    is_published: bool = True


class TestimonialUpdateRequest(BaseModel):
    client_name: str | None = None
    company: str | None = None
    quote: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    avatar_url: str | None = None
    is_published: bool | None = None


class TestimonialResponse(BaseModel):
    id: uuid.UUID
    client_name: str | None
    company: str | None
    quote: str | None
    rating: int | None
    avatar_url: str | None
    is_published: bool

    model_config = {"from_attributes": True}


class FaqCreateRequest(BaseModel):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    category: str | None = None
    display_order: int | None = None
    is_published: bool = True


class FaqUpdateRequest(BaseModel):
    question: str | None = None
    answer: str | None = None
    category: str | None = None
    display_order: int | None = None
    is_published: bool | None = None


class FaqResponse(BaseModel):
    id: uuid.UUID
    question: str | None
    answer: str | None
    category: str | None
    display_order: int | None
    is_published: bool

    model_config = {"from_attributes": True}


class BlogPostCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    slug: str = slug_field()
    excerpt: str | None = None
    content: str | None = None
    cover_image_url: str | None = None
    tags: list[str] | None = None
    is_published: bool = False
    published_at: datetime | None = None
    expires_at: datetime | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    keywords: list[str] | None = None
    topic_tag: str | None = None


class BlogPostUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    slug: str | None = slug_field(required=False)
    excerpt: str | None = None
    content: str | None = None
    cover_image_url: str | None = None
    tags: list[str] | None = None
    is_published: bool | None = None
    published_at: datetime | None = None
    expires_at: datetime | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    keywords: list[str] | None = None
    topic_tag: str | None = None


class BlogPostResponse(BaseModel):
    id: uuid.UUID
    title: str | None
    slug: str
    excerpt: str | None
    content: str | None
    cover_image_url: str | None
    author_id: uuid.UUID | None
    tags: list[str] | None
    is_published: bool
    published_at: datetime | None
    expires_at: datetime | None
    meta_title: str | None
    meta_description: str | None
    keywords: list[str] | None
    topic_tag: str | None
    word_count: int | None

    model_config = {"from_attributes": True}
