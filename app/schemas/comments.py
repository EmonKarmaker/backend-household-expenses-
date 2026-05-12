"""Comment request/response schemas."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CommentCreate(BaseModel):
    entry_type: Literal["shopping", "bill", "asset"]
    entry_id: int
    body: str


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_type: str
    entry_id: int
    user_id: int
    body: str
    created_at: datetime
