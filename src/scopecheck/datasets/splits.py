"""Release-frozen synthetic splits. Changing labels requires a new manifest version."""

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Literal

from scopecheck.datasets.canonicalize import canonical_json, content_hash, parse_dataset
from scopecheck.datasets.schemas import Dataset

MANIFEST_HASH = "605057160a4923ecb5f6d48868760f975cff3b21b24b6b34a549b65831f5c2ea"


def read_release_file(relative: str) -> str:
    source = Path(__file__).resolve().parents[3] / "datasets"
    if source.is_dir():
        return (source / relative).read_text(encoding="utf-8")
    return files("scopecheck").joinpath("data", relative).read_text(encoding="utf-8")


def labels_hash(dataset: Dataset) -> str:
    labels = {
        case.case_id: case.expected.model_dump(mode="json") for case in dataset.cases
    }
    return hashlib.sha256(canonical_json(labels).encode()).hexdigest()


def frozen_dataset(split: Literal["dev", "holdout"]) -> Dataset:
    manifest = json.loads(read_release_file("manifest-v1.json"))
    if hashlib.sha256(canonical_json(manifest).encode()).hexdigest() != MANIFEST_HASH:
        raise ValueError("Frozen split manifest does not match this release")
    entry = manifest["splits"][split]
    dataset = parse_dataset(read_release_file(f"{split}/suite.json"))
    if (
        content_hash(dataset) != entry["content_hash"]
        or labels_hash(dataset) != entry["labels_hash"]
        or len(dataset.cases) != entry["case_count"]
        or sorted(case.case_id for case in dataset.cases) != entry["case_ids"]
    ):
        raise ValueError("Frozen dataset or labels have changed")
    return dataset
