"""Comment request/response schemas."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CommentCreate(BaseModel):
    entry_type: Literal["shopping", "bill", "asset"]
    entry_id: int
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body", mode="before")
    @classmethod
    def strip_body(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("body must not be empty or whitespace")
        return stripped


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_type: str
    entry_id: int
    user_id: int
    body: str
    created_at: datetime
