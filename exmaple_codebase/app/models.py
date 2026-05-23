from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80, examples=["widget"])


class Item(BaseModel):
    id: int
    name: str

