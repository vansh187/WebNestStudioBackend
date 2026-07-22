from pydantic import BaseModel, EmailStr


class NewsletterSubscribeRequest(BaseModel):
    email: EmailStr
    source: str | None = None


class NewsletterSubscribeResponse(BaseModel):
    email: str
    subscribed: bool

    model_config = {"from_attributes": True}
