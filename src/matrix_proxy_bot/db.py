"""Session database and tracking."""

import aiosqlite
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SessionDB:
    """Simple session tracking: room_id ↔ agent session."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.initialized = False

    async def initialize(self):
        """Create tables if needed."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    room_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    owner TEXT DEFAULT 'matrix',
                    created_at TEXT,
                    last_message_at TEXT
                )
                """
            )
            await db.commit()
        self.initialized = True
        logger.info(f"Initialized session DB at {self.db_path}")

    async def get_session(self, room_id: str) -> Optional[dict]:
        """Get session record for a room."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE room_id = ?", (room_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_session(self, room_id: str, session_id: str) -> None:
        """Create new session record."""
        async with aiosqlite.connect(self.db_path) as db:
            now = datetime.utcnow().isoformat()
            await db.execute(
                """
                INSERT OR REPLACE INTO sessions 
                (room_id, session_id, owner, created_at, last_message_at)
                VALUES (?, ?, 'matrix', ?, ?)
                """,
                (room_id, session_id, now, now),
            )
            await db.commit()
        logger.info(f"Created session for room {room_id}: {session_id}")

    async def set_owner(self, room_id: str, owner: str) -> None:
        """Set session owner (matrix or emacs)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET owner = ?, last_message_at = ? WHERE room_id = ?",
                (owner, datetime.utcnow().isoformat(), room_id),
            )
            await db.commit()
        logger.info(f"Changed owner for room {room_id} to {owner}")

    async def get_owner(self, room_id: str) -> str:
        """Get current owner of a session."""
        session = await self.get_session(room_id)
        return session.get("owner", "matrix") if session else "matrix"

    async def touch(self, room_id: str) -> None:
        """Update last_message_at timestamp."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET last_message_at = ? WHERE room_id = ?",
                (datetime.utcnow().isoformat(), room_id),
            )
            await db.commit()
