from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    google_id TEXT UNIQUE,
                    auth_provider TEXT NOT NULL DEFAULT 'local',
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
            if "google_id" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN google_id TEXT")
            if "auth_provider" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'local'")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
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
                    user_id INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns(session_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_user ON chat_turns(user_id)")
            connection.commit()

    @staticmethod
    def _hash_password(password: str, salt: bytes | None = None) -> str:
        real_salt = salt or os.urandom(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), real_salt, 120_000)
        return f"{real_salt.hex()}:{derived.hex()}"

    @staticmethod
    def _verify_password(password: str, stored_hash: str) -> bool:
        try:
            salt_hex, digest_hex = stored_hash.split(":", 1)
        except ValueError:
            return False
        computed = Database._hash_password(password, bytes.fromhex(salt_hex))
        return secrets.compare_digest(computed, stored_hash)

    async def create_user(self, name: str, email: str, password: str, created_at: str) -> dict[str, Any]:
        password_hash = self._hash_password(password)

        def work() -> dict[str, Any]:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO users(name, email, password_hash, google_id, auth_provider, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, email.lower(), password_hash, None, "local", created_at),
                )
                connection.commit()
                return {"id": cursor.lastrowid, "name": name, "email": email.lower(), "created_at": created_at}

        return await asyncio.to_thread(work)

    async def authenticate_user(self, email: str, password: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT id, name, email, password_hash, created_at FROM users WHERE email = ?",
                    (email.lower(),),
                ).fetchone()
            if not row or not self._verify_password(password, row["password_hash"]):
                return None
            return {"id": row["id"], "name": row["name"], "email": row["email"], "created_at": row["created_at"]}

        return await asyncio.to_thread(work)

    async def get_or_create_google_user(self, google_id: str, name: str, email: str, created_at: str) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT id, name, email, created_at FROM users WHERE google_id = ? OR email = ?",
                    (google_id, email.lower()),
                ).fetchone()
                if row:
                    connection.execute(
                        "UPDATE users SET google_id = ?, auth_provider = 'google', name = ?, email = ? WHERE id = ?",
                        (google_id, name, email.lower(), row["id"]),
                    )
                    connection.commit()
                    return {"id": row["id"], "name": name, "email": email.lower(), "created_at": row["created_at"]}
                cursor = connection.execute(
                    "INSERT INTO users(name, email, password_hash, google_id, auth_provider, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, email.lower(), "", google_id, "google", created_at),
                )
                connection.commit()
                return {"id": cursor.lastrowid, "name": name, "email": email.lower(), "created_at": created_at}

        return await asyncio.to_thread(work)

    async def create_auth_session(self, user_id: int, created_at: str) -> str:
        token = secrets.token_urlsafe(32)

        def work() -> str:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO auth_sessions(token, user_id, created_at) VALUES (?, ?, ?)",
                    (token, user_id, created_at),
                )
                connection.commit()
            return token

        return await asyncio.to_thread(work)

    async def get_user_by_token(self, token: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT users.id, users.name, users.email, users.created_at
                    FROM auth_sessions
                    JOIN users ON users.id = auth_sessions.user_id
                    WHERE auth_sessions.token = ?
                    """,
                    (token,),
                ).fetchone()
            if not row:
                return None
            return {"id": row["id"], "name": row["name"], "email": row["email"], "created_at": row["created_at"]}

        return await asyncio.to_thread(work)

    async def delete_auth_session(self, token: str) -> None:
        def work() -> None:
            with self._connect() as connection:
                connection.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
                connection.commit()

        await asyncio.to_thread(work)
