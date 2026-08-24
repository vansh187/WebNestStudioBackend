from pydantic import BaseModel, EmailStr


class EmailTestRequest(BaseModel):
    to_address: EmailStr | None = None


class EmailTestResponse(BaseModel):
    sent: bool
    to_address: str
    provider: str
    from_address: str
    api_key_configured: bool
    detail: str
