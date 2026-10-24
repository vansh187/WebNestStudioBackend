"""Response models for sections 2.1 and 2.2, field for field."""

from datetime import datetime

from pydantic import BaseModel


class RelayLimits(BaseModel):
    per_minute: int
    per_day: int
    max_request_bytes: int
    max_response_bytes: int
    timeout_seconds: int


class ProviderSummary(BaseModel):
    id: str
    name: str
    hosts: list[str]
    test_mode: str
    test_key_hint: str
    docs_url: str


class ProvidersResponse(BaseModel):
    enabled: bool
    relay_path: str
    limits: RelayLimits
    providers: list[ProviderSummary]


class UsageResponse(BaseModel):
    minute_remaining: int
    day_remaining: int
    day_resets_at: datetime
