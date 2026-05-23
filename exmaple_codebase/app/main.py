from __future__ import annotations

from fastapi import FastAPI

from .models import HealthResponse, Item, ItemCreate
from .services import ItemService

app = FastAPI(title="Example API")
service = ItemService()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/items", response_model=Item)
def create_item(payload: ItemCreate) -> Item:
    return service.create_item(payload)


@app.get("/items/{item_id}", response_model=Item)
def get_item(item_id: int) -> Item:
    return service.get_item(item_id)

