"""JSONL session logs: one line per event, used for evaluation in Phase 6."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_FIELD_CHARS = 4000


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"... [{len(value) - MAX_FIELD_CHARS} chars trimmed]"
    return value


class SessionLogger:
    def __init__(self, log_dir: str | Path = "logs", session_id: str | None = None) -> None:
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S")
        self.path = Path(log_dir) / f"session-{self.session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": self.session_id,
            "event": event,
            **{k: _trim(v) for k, v in fields.items()},
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
