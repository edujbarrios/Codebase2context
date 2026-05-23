from __future__ import annotations

from .db import InMemoryDB
from .models import Item, ItemCreate


class ItemService:
    def __init__(self) -> None:
        self._db = InMemoryDB()

    def create_item(self, payload: ItemCreate) -> Item:
        row = self._db.insert_item(payload.name)
        return Item(id=row.id, name=row.name)

    def get_item(self, item_id: int) -> Item:
        row = self._db.get_item(item_id)
        if row is None:
            return Item(id=item_id, name="(missing)")
        return Item(id=row.id, name=row.name)

