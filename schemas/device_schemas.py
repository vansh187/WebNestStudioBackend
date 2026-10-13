from typing import Literal

from pydantic import BaseModel, Field


class DeviceTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
    platform: Literal["android", "ios"] = "android"


class DeviceTokenDeleteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
