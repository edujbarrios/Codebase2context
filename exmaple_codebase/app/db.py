from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ItemRow:
    id: int
    name: str


class InMemoryDB:
    def __init__(self) -> None:
        self._items: dict[int, ItemRow] = {}
        self._next_id = 1

    def insert_item(self, name: str) -> ItemRow:
        row = ItemRow(id=self._next_id, name=name)
        self._items[row.id] = row
        self._next_id += 1
        return row

    def get_item(self, item_id: int) -> ItemRow | None:
        return self._items.get(item_id)

