"""Load and validate frozen item sets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from pecker_drum.items.schema import Item


class ItemSetError(ValueError):
    """An item set file is malformed."""


@dataclass(frozen=True, slots=True)
class ItemSet:
    name: str
    path: Path
    digest: str
    """SHA-256 of the file, so a run can tell when the set changed."""
    items: tuple[Item, ...]

    def get(self, item_id: str) -> Item:
        for item in self.items:
            if item.id == item_id:
                return item
        msg = f"no item {item_id!r} in set {self.name!r}"
        raise KeyError(msg)


def resolve_set_path(name_or_path: str, items_dir: Path) -> Path:
    """``public_v1`` -> ``items/public_v1.jsonl``; an existing path is used as is."""
    candidate = Path(name_or_path)
    if candidate.suffix == ".jsonl" and candidate.is_file():
        return candidate
    path = items_dir / f"{name_or_path}.jsonl"
    if not path.is_file():
        msg = f"item set {name_or_path!r} not found (looked for {path})"
        raise ItemSetError(msg)
    return path


def load_item_set(path: Path) -> ItemSet:
    """Parse a JSONL item set. Unknown fields, bad values and duplicate ids are errors."""
    raw = path.read_bytes()
    items: list[Item] = []
    seen: set[str] = set()
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = Item.model_validate_json(line)
        except ValidationError as exc:
            msg = f"{path}:{line_no}: invalid item: {exc}"
            raise ItemSetError(msg) from exc
        if item.id in seen:
            msg = f"{path}:{line_no}: duplicate item id {item.id!r}"
            raise ItemSetError(msg)
        seen.add(item.id)
        items.append(item)
    if not items:
        msg = f"{path}: item set is empty"
        raise ItemSetError(msg)
    return ItemSet(
        name=path.stem,
        path=path,
        digest=hashlib.sha256(raw).hexdigest(),
        items=tuple(items),
    )
