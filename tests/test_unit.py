"""Unit tests for command parsing, DB operations, and send queue."""

import asyncio
import json
import pytest
import pytest_asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from matrix_proxy_bot.bot import CommandParser
from matrix_proxy_bot.db import SessionDB


# --- CommandParser tests ---

class TestCommandParser:
    def test_regular_message(self):
        result = CommandParser.parse("hello world")
        assert result["is_command"] is False
        assert result["raw"] == "hello world"

    def test_return_command(self):
        result = CommandParser.parse("!return")
        assert result["is_command"] is True
        assert result["action"] == "handoff_end"

    def test_status_command(self):
        result = CommandParser.parse("!status")
        assert result["is_command"] is True
        assert result["action"] == "session_status"

    def test_help_command(self):
        result = CommandParser.parse("!help")
        assert result["is_command"] is True
        assert result["action"] == "help"

    def test_close_command(self):
        result = CommandParser.parse("!close")
        assert result["is_command"] is True
        assert result["action"] == "close_session"

    def test_unknown_command(self):
        result = CommandParser.parse("!bogus")
        assert result["is_command"] is True
        assert result["action"] is None
        assert "error" in result

    def test_command_with_args(self):
        result = CommandParser.parse("!close now please")
        assert result["is_command"] is True
        assert result["args"] == ["now", "please"]

    def test_whitespace_stripped(self):
        result = CommandParser.parse("  !return  ")
        assert result["is_command"] is True
        assert result["action"] == "handoff_end"


# --- SessionDB tests ---

@pytest_asyncio.fixture
async def db():
    """Create a temporary SessionDB for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        session_db = SessionDB(db_path)
        await session_db.initialize()
        yield session_db


@pytest.mark.asyncio
class TestSessionDB:
    async def test_create_and_get_session(self, db):
        await db.create_session(
            room_id="!room:test",
            session_id="sess-1",
            session_hash="abc123",
            hostname="myhost",
            webhook_url="http://localhost:9999/webhook",
            webhook_secret="secret",
        )
        session = await db.get_session("!room:test")
        assert session is not None
        assert session["session_id"] == "sess-1"
        assert session["hostname"] == "myhost"
        assert session["owner"] == "matrix"

    async def test_get_nonexistent_session(self, db):
        session = await db.get_session("!nonexistent:test")
        assert session is None

    async def test_set_owner(self, db):
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://x", webhook_secret="s",
        )
        await db.set_owner("!room:test", "emacs")
        session = await db.get_session("!room:test")
        assert session["owner"] == "emacs"

    async def test_create_session_with_ttl(self, db):
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://x", webhook_secret="s",
            ttl_seconds=3600,
        )
        session = await db.get_session("!room:test")
        assert session["ttl_seconds"] == 3600
        assert session["handoff_expires_at"] is not None

    async def test_touch_uses_stored_ttl(self, db):
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://x", webhook_secret="s",
            ttl_seconds=1800,  # 30 minutes
        )
        session_before = await db.get_session("!room:test")
        await db.touch("!room:test")
        session_after = await db.get_session("!room:test")
        # TTL should have been reset, so expires_at should be later
        assert session_after["handoff_expires_at"] >= session_before["handoff_expires_at"]
        assert session_after["ttl_seconds"] == 1800

    async def test_create_session_with_capabilities(self, db):
        modes = ["Plan", "Agent", "Autopilot"]
        models = ["claude-sonnet-4-5", "claude-opus-4"]
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://x", webhook_secret="s",
            available_modes=modes, current_mode="Agent",
            available_models=models, current_model="claude-sonnet-4-5",
        )
        session = await db.get_session("!room:test")
        assert json.loads(session["available_modes"]) == modes
        assert session["current_mode"] == "Agent"
        assert json.loads(session["available_models"]) == models
        assert session["current_model"] == "claude-sonnet-4-5"

    async def test_update_webhook_with_capabilities(self, db):
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://old", webhook_secret="old",
        )
        await db.update_webhook(
            "!room:test",
            webhook_url="http://new", webhook_secret="new",
            available_modes=["Plan", "Agent"],
            current_mode="Plan",
            available_models=["gpt-4"],
            current_model="gpt-4",
        )
        session = await db.get_session("!room:test")
        assert session["agent_shell_webhook_url"] == "http://new"
        assert session["agent_shell_secret"] == "new"
        assert session["current_mode"] == "Plan"
        assert session["current_model"] == "gpt-4"

    async def test_update_current(self, db):
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://x", webhook_secret="s",
            current_mode="Agent", current_model="claude-sonnet-4-5",
        )
        await db.update_current("!room:test", mode="Plan")
        session = await db.get_session("!room:test")
        assert session["current_mode"] == "Plan"
        assert session["current_model"] == "claude-sonnet-4-5"  # unchanged

        await db.update_current("!room:test", model="claude-opus-4")
        session = await db.get_session("!room:test")
        assert session["current_mode"] == "Plan"  # unchanged
        assert session["current_model"] == "claude-opus-4"

    async def test_find_session_by_id(self, db):
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://x", webhook_secret="s",
        )
        found = await db.find_session_by_id("s1", "host")
        assert found is not None
        assert found["room_id"] == "!room:test"

        not_found = await db.find_session_by_id("s1", "other-host")
        assert not_found is None

    async def test_expired_sessions(self, db):
        # Create with very short TTL that's already expired
        await db.create_session(
            room_id="!room:test", session_id="s1", session_hash="h1",
            hostname="host", webhook_url="http://x", webhook_secret="s",
            ttl_seconds=1,
        )
        # Wait for expiry
        await asyncio.sleep(1.1)
        expired = await db.get_expired_sessions()
        assert len(expired) == 1
        assert expired[0]["room_id"] == "!room:test"

    async def test_schema_migration_idempotent(self, db):
        """Calling initialize twice should not fail."""
        await db.initialize()
        session = await db.get_session("!nonexistent:test")
        assert session is None


# --- Dynamic command resolution tests ---

class TestDynamicCommands:
    """Test the dynamic command matching logic extracted from _try_dynamic_command."""

    def _make_session(self, modes=None, models=None, current_mode=None, current_model=None):
        session = {
            "available_modes": json.dumps(modes) if modes else None,
            "available_models": json.dumps(models) if models else None,
            "current_mode": current_mode,
            "current_model": current_model,
            "agent_shell_webhook_url": "http://localhost:9999",
            "agent_shell_secret": "test",
        }
        return session

    def test_mode_command_match(self):
        session = self._make_session(modes=["Plan", "Agent", "Autopilot"])
        modes = json.loads(session["available_modes"])
        mode_map = {m.lower(): m for m in modes}
        assert mode_map.get("plan") == "Plan"
        assert mode_map.get("autopilot") == "Autopilot"
        assert mode_map.get("bogus") is None

    def test_model_partial_match(self):
        models = ["claude-sonnet-4-5", "claude-opus-4", "gpt-4.1"]
        target = "opus"
        match = next((m for m in models if m.startswith(target) or target in m), None)
        assert match == "claude-opus-4"

    def test_model_exact_match(self):
        models = ["claude-sonnet-4-5", "claude-opus-4"]
        target = "claude-opus-4"
        match = next((m for m in models if m.startswith(target) or target in m), None)
        assert match == "claude-opus-4"

    def test_model_no_match(self):
        models = ["claude-sonnet-4-5", "claude-opus-4"]
        target = "gemini"
        match = next((m for m in models if m.startswith(target) or target in m), None)
        assert match is None

    def test_model_prefix_match(self):
        models = ["claude-sonnet-4-5", "claude-opus-4", "gpt-4.1"]
        target = "gpt"
        match = next((m for m in models if m.startswith(target) or target in m), None)
        assert match == "gpt-4.1"


# --- Sync loop tests ---

class TestSyncLoop:
    """Verify _sync_loop handles errors with backoff and exits on auth failure."""

    def _make_bot(self):
        """Build a ProxyBot instance without invoking __init__ (avoids FastAPI/Config wiring)."""
        from matrix_proxy_bot.bot import ProxyBot
        bot = ProxyBot.__new__(ProxyBot)
        bot.client = MagicMock()
        return bot

    @pytest.mark.asyncio
    async def test_exits_on_unknown_token(self):
        from matrix_proxy_bot.bot import ProxyBot
        from nio.responses import SyncError

        bot = self._make_bot()
        bot.client.sync = AsyncMock(return_value=SyncError(
            message="Invalid access token", status_code="M_UNKNOWN_TOKEN"
        ))

        # Should return cleanly without hitting sleep — finishes in well under a second.
        await asyncio.wait_for(bot._sync_loop(), timeout=1.0)
        assert bot.client.sync.await_count == 1

    @pytest.mark.asyncio
    async def test_exits_on_soft_logout(self):
        from nio.responses import SyncError

        bot = self._make_bot()
        bot.client.sync = AsyncMock(return_value=SyncError(
            message="Soft logout", status_code="M_FORBIDDEN", soft_logout=True
        ))

        await asyncio.wait_for(bot._sync_loop(), timeout=1.0)
        assert bot.client.sync.await_count == 1

    @pytest.mark.asyncio
    async def test_backoff_on_transient_error_then_recover(self, monkeypatch):
        from matrix_proxy_bot.bot import ProxyBot
        from nio.responses import SyncError, SyncResponse

        bot = self._make_bot()

        # Shrink bounds so the test is fast.
        monkeypatch.setattr(ProxyBot, "SYNC_BACKOFF_INITIAL", 0.01)
        monkeypatch.setattr(ProxyBot, "SYNC_BACKOFF_MAX", 0.04)

        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(d):
            sleeps.append(d)
            await real_sleep(0)

        monkeypatch.setattr("matrix_proxy_bot.bot.asyncio.sleep", fake_sleep)

        # Two transient errors, one success, then a fatal to terminate the loop.
        ok = SyncResponse(
            next_batch="s1", rooms=MagicMock(), device_key_count={},
            device_list=MagicMock(), to_device_events=[], presence_events=[],
        )
        responses = [
            SyncError(message="boom1", status_code="M_UNKNOWN"),
            SyncError(message="boom2", status_code="M_UNKNOWN"),
            ok,
            SyncError(message="bye", status_code="M_UNKNOWN_TOKEN"),
        ]
        bot.client.sync = AsyncMock(side_effect=responses)

        await asyncio.wait_for(bot._sync_loop(), timeout=1.0)

        # Two transient errors → sleeps of 0.01 then 0.02 (exponential).
        # Success resets, fatal exits without sleeping.
        assert sleeps == [0.01, 0.02]
        assert bot.client.sync.await_count == 4

    @pytest.mark.asyncio
    async def test_backoff_caps_at_max(self, monkeypatch):
        from matrix_proxy_bot.bot import ProxyBot
        from nio.responses import SyncError

        bot = self._make_bot()
        monkeypatch.setattr(ProxyBot, "SYNC_BACKOFF_INITIAL", 0.01)
        monkeypatch.setattr(ProxyBot, "SYNC_BACKOFF_MAX", 0.03)

        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(d):
            sleeps.append(d)
            await real_sleep(0)

        monkeypatch.setattr("matrix_proxy_bot.bot.asyncio.sleep", fake_sleep)

        errs = [SyncError(message="x", status_code="M_UNKNOWN") for _ in range(5)]
        errs.append(SyncError(message="bye", status_code="M_UNKNOWN_TOKEN"))
        bot.client.sync = AsyncMock(side_effect=errs)

        await asyncio.wait_for(bot._sync_loop(), timeout=1.0)

        # 0.01, 0.02, 0.03 (capped), 0.03, 0.03 — fatal at the end exits without sleeping.
        assert sleeps == [0.01, 0.02, 0.03, 0.03, 0.03]

    @pytest.mark.asyncio
    async def test_backoff_on_exception(self, monkeypatch):
        from matrix_proxy_bot.bot import ProxyBot
        from nio.responses import SyncError

        bot = self._make_bot()
        monkeypatch.setattr(ProxyBot, "SYNC_BACKOFF_INITIAL", 0.01)
        monkeypatch.setattr(ProxyBot, "SYNC_BACKOFF_MAX", 0.04)

        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(d):
            sleeps.append(d)
            await real_sleep(0)

        monkeypatch.setattr("matrix_proxy_bot.bot.asyncio.sleep", fake_sleep)

        bot.client.sync = AsyncMock(side_effect=[
            ConnectionError("network down"),
            ConnectionError("still down"),
            SyncError(message="bye", status_code="M_UNKNOWN_TOKEN"),
        ])

        await asyncio.wait_for(bot._sync_loop(), timeout=1.0)
        assert sleeps == [0.01, 0.02]
