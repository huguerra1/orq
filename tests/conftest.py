from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_fixture(relative_path: str) -> Any:
    with (ROOT / "fixtures" / "v0.1" / relative_path).open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def valid_task() -> dict[str, Any]:
    return load_fixture("valid/task-minimal.json")


@pytest.fixture
def valid_workflow() -> dict[str, Any]:
    return load_fixture("valid/workflow-minimal.json")
