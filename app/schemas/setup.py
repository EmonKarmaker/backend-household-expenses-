"""Setup / first-run schemas."""
from pydantic import BaseModel, EmailStr


class SetupRequiredResponse(BaseModel):
    required: bool


class SetupInitRequest(BaseModel):
    household_name: str
    admin_name: str
    admin_email: EmailStr
    admin_password: str
