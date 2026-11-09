"""Hash validated content, including version; whitespace/key order are irrelevant."""

import hashlib
import json
from pathlib import Path
from typing import Any

from scopecheck.datasets.schemas import Dataset


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(dataset: Dataset) -> str:
    return hashlib.sha256(
        canonical_json(dataset.model_dump(mode="json")).encode()
    ).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def parse_dataset(raw: str) -> Dataset:
    data = json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    return Dataset.model_validate(data)


def load_dataset(path: Path) -> Dataset:
    return parse_dataset(path.read_text(encoding="utf-8"))
