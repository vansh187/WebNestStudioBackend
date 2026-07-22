from pydantic import BaseModel, EmailStr


class EmailTestRequest(BaseModel):
    to_address: EmailStr | None = None


class EmailTestResponse(BaseModel):
    sent: bool
    to_address: str
    smtp_host: str
    smtp_port: int
    smtp_user_configured: bool
    detail: str
