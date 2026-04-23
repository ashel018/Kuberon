from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from agent.types import ChatTurn

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore[assignment]


class ConversationMemory:
    def __init__(self, redis_url: str | None = None, sqlite_path: str | Path | None = None) -> None:
        self._redis_url = redis_url
        self._redis = Redis.from_url(redis_url, decode_responses=True) if redis_url and Redis else None
        self._fallback: dict[str, list[dict]] = defaultdict(list)
        self._sqlite_path = Path(sqlite_path) if sqlite_path else None
        if self._sqlite_path:
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            import sqlite3

            with sqlite3.connect(self._sqlite_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        user_message TEXT NOT NULL,
                        assistant_message TEXT NOT NULL,
                        tool_calls_json TEXT NOT NULL,
                        reasoning_steps_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        user_id INTEGER
                    )
                    """
                )
                connection.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns(session_id)")
                connection.commit()

    async def append_turn(self, session_id: str, turn: ChatTurn) -> None:
        payload = json.dumps(asdict(turn))
        if self._redis:
            try:
                await self._redis.rpush(f"kubeops:session:{session_id}", payload)
                await self._redis.expire(f"kubeops:session:{session_id}", 60 * 60 * 24 * 7)
                return
            except Exception:
                pass
        if self._sqlite_path:
            import sqlite3

            def work() -> None:
                turn_dict = asdict(turn)
                with sqlite3.connect(self._sqlite_path) as connection:
                    connection.execute(
                        """
                        INSERT INTO chat_turns (
                            session_id,
                            namespace,
                            user_message,
                            assistant_message,
                            tool_calls_json,
                            reasoning_steps_json,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            turn.namespace,
                            turn.user_message,
                            turn.assistant_message,
                            json.dumps(turn_dict["tool_calls"]),
                            json.dumps(turn_dict["reasoning_steps"]),
                            turn.created_at,
                        ),
                    )
                    connection.commit()

            import asyncio

            await asyncio.to_thread(work)
            return
        self._fallback[session_id].append(json.loads(payload))

    @staticmethod
    def _derive_title(message: str) -> str:
        text = " ".join((message or "").split()).strip()
        if not text:
            return "Untitled chat"

        lowered = text.lower()
        prefixes = [
            "what is ",
            "what are ",
            "how does ",
            "how do i ",
            "show me ",
            "can you ",
            "please ",
            "why is ",
            "why does ",
            "tell me about ",
            "explain ",
        ]
        for prefix in prefixes:
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                break

        text = text.rstrip("?.! ")
        words = text.split()
        if not words:
            return "Untitled chat"
        return " ".join(words[:7]).strip().capitalize()

    async def get_turns(self, session_id: str, limit: int = 8) -> list[dict]:
        if self._redis:
            try:
                values = await self._redis.lrange(f"kubeops:session:{session_id}", max(-limit, -100), -1)
                return [json.loads(item) for item in values]
            except Exception:
                pass
        if self._sqlite_path:
            import sqlite3
            import asyncio

            def work() -> list[dict]:
                with sqlite3.connect(self._sqlite_path) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = connection.execute(
                        """
                        SELECT user_message, assistant_message, namespace, tool_calls_json, reasoning_steps_json, created_at
                        FROM chat_turns
                        WHERE session_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (session_id, limit),
                    ).fetchall()
                items = []
                for row in reversed(rows):
                    items.append(
                        {
                            "user_message": row["user_message"],
                            "assistant_message": row["assistant_message"],
                            "namespace": row["namespace"],
                            "tool_calls": json.loads(row["tool_calls_json"]),
                            "reasoning_steps": json.loads(row["reasoning_steps_json"]),
                            "created_at": row["created_at"],
                        }
                    )
                return items

            return await asyncio.to_thread(work)
        return self._fallback.get(session_id, [])[-limit:]

    async def list_sessions(self) -> list[dict]:
        if self._redis:
            try:
                keys = await self._redis.keys("kubeops:session:*")
                session_ids = sorted(key.split(":")[-1] for key in keys)
                items: list[dict] = []
                for session_id in session_ids:
                    turns = await self.get_turns(session_id, limit=50)
                    title = session_id
                    preview = "Open this conversation again."
                    updated_at = ""
                    if turns:
                        title = self._derive_title(turns[0]["user_message"])
                        preview = turns[-1]["assistant_message"][:140]
                        updated_at = turns[-1]["created_at"]
                    items.append(
                        {
                            "session_id": session_id,
                            "title": title,
                            "preview": preview,
                            "updated_at": updated_at,
                        }
                    )
                return items
            except Exception:
                pass
        if self._sqlite_path:
            import sqlite3
            import asyncio

            def work() -> list[dict]:
                with sqlite3.connect(self._sqlite_path) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = connection.execute(
                        """
                        SELECT
                            latest.session_id AS session_id,
                            first_turn.user_message AS title,
                            latest.assistant_message AS preview,
                            latest.created_at AS updated_at
                        FROM chat_turns AS latest
                        INNER JOIN (
                            SELECT session_id, MAX(id) AS max_id
                            FROM chat_turns
                            GROUP BY session_id
                        ) AS grouped
                            ON grouped.session_id = latest.session_id
                           AND grouped.max_id = latest.id
                        INNER JOIN (
                            SELECT session_id, MIN(id) AS min_id
                            FROM chat_turns
                            GROUP BY session_id
                        ) AS starters
                            ON starters.session_id = latest.session_id
                        INNER JOIN chat_turns AS first_turn
                            ON first_turn.session_id = starters.session_id
                           AND first_turn.id = starters.min_id
                        ORDER BY latest.id DESC
                        """
                    ).fetchall()
                return [
                    {
                        "session_id": row["session_id"],
                        "title": ConversationMemory._derive_title(row["title"]),
                        "preview": row["preview"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]

            return await asyncio.to_thread(work)
        return [
            {
                "session_id": session_id,
                "title": self._derive_title(turns[0]["user_message"]) if turns else session_id,
                "preview": turns[-1]["assistant_message"][:140] if turns else "Open this conversation again.",
                "updated_at": turns[-1]["created_at"] if turns else "",
            }
            for session_id, turns in sorted(self._fallback.items())
        ]

    async def delete_session(self, session_id: str) -> None:
        if self._redis:
            try:
                await self._redis.delete(f"kubeops:session:{session_id}")
            except Exception:
                pass
        if self._sqlite_path:
            import sqlite3
            import asyncio

            def work() -> None:
                with sqlite3.connect(self._sqlite_path) as connection:
                    connection.execute("DELETE FROM chat_turns WHERE session_id = ?", (session_id,))
                    connection.commit()

            await asyncio.to_thread(work)
        self._fallback.pop(session_id, None)

    @staticmethod
    def summarize(turns: Iterable[dict]) -> str:
        lines: list[str] = []
        for turn in turns:
            lines.append(f"User: {turn['user_message']}")
            lines.append(f"Assistant: {turn['assistant_message']}")
        return "\n".join(lines)
