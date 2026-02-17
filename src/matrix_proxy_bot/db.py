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
                    session_id TEXT NOT NULL,
                    session_hash TEXT,
                    hostname TEXT,
                    owner TEXT DEFAULT 'matrix',
                    initiated_by TEXT,
                    initiated_at TEXT,
                    created_at TEXT,
                    last_message_at TEXT,
                    agent_shell_webhook_url TEXT,
                    agent_shell_secret TEXT,
                    quiet_mode BOOLEAN DEFAULT 0,
                    handoff_expires_at TEXT
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

    async def create_session(
        self,
        room_id: str,
        session_id: str,
        session_hash: str,
        hostname: str,
        webhook_url: str,
        webhook_secret: str,
        quiet_mode: bool = False,
        ttl_seconds: Optional[int] = None,
        initiated_by: Optional[str] = None,
    ) -> None:
        """Create new handoff session record."""
        now = datetime.utcnow().isoformat()
        expires_at = None
        if ttl_seconds:
            from datetime import timedelta
            expires_at = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO sessions 
                (room_id, session_id, session_hash, hostname, owner, initiated_by, 
                 initiated_at, created_at, last_message_at, agent_shell_webhook_url, 
                 agent_shell_secret, quiet_mode, handoff_expires_at)
                VALUES (?, ?, ?, ?, 'matrix', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    session_id,
                    session_hash,
                    hostname,
                    initiated_by,
                    now,
                    now,
                    now,
                    webhook_url,
                    webhook_secret,
                    quiet_mode,
                    expires_at,
                ),
            )
            await db.commit()
        logger.info(f"Created handoff session for {hostname}-{session_hash} in {room_id}")

    async def set_owner(self, room_id: str, owner: str) -> None:
        """Set session owner (matrix or emacs)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET owner = ?, last_message_at = ? WHERE room_id = ?",
                (owner, datetime.utcnow().isoformat(), room_id),
            )
            await db.commit()
        logger.info(f"Changed owner for room {room_id} to {owner}")

    async def touch(self, room_id: str) -> None:
        """Update last_message_at timestamp for a session."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET last_message_at = ? WHERE room_id = ?",
                (datetime.utcnow().isoformat(), room_id),
            )
            await db.commit()

    async def get_owner(self, room_id: str) -> str:
        """Get current owner of a session."""
        session = await self.get_session(room_id)
        return session.get("owner", "matrix") if session else "matrix"

    async def list_sessions(self) -> list[dict]:
        """List all active sessions."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM sessions WHERE owner = 'matrix'")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_expired_sessions(self) -> list[dict]:
        """Get sessions with expired TTL."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM sessions 
                WHERE owner = 'matrix' 
                AND handoff_expires_at IS NOT NULL 
                AND handoff_expires_at < ?
                """,
                (now,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
