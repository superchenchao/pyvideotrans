from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Mapping


def read_state(path: str | Path) -> dict[str, object]:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: str | Path, values: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(values)
    payload["updated_at"] = int(time.time())
    temporary = Path(f"{path}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def matching_state(
        path: str | Path, expected_identity: Mapping[str, object]
) -> dict[str, object]:
    state = read_state(path)
    identity = state.get("identity")
    return state if identity == dict(expected_identity) else {}
