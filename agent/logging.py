from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any


class StructuredRunLogger:
    def __init__(self, log_dir: str | Path) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.log_dir / "reasoning.jsonl"
        self.sqlite_path = self.log_dir / "reasoning.sqlite3"
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reasoning_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_ts TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def append(self, session_id: str, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload, ensure_ascii=True)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                "INSERT INTO reasoning_log (session_id, turn_ts, payload) VALUES (?, ?, ?)",
                (session_id, payload["created_at"], serialized),
            )

    @staticmethod
    def normalize(obj: Any) -> Any:
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        return obj

