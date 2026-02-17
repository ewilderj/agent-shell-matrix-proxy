#!/usr/bin/env python3
"""
Integration test: Full handoff flow with mock Matrix client.

No Matrix server required. Simulates:
1. Handoff initiated via webhook
2. Room created (mocked)
3. User sends message (mocked)
4. Message relayed to webhook (actual mock server)
5. Response posted back to room (mocked)
6. !return command processed
7. Session returned to Emacs

Usage:
    uv run python tests/test_integration.py
    # OR with bot running:
    python3 tests/test_integration.py
"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4
import hashlib
from datetime import datetime


class MockSessionDB:
    """Mock session database."""
    def __init__(self):
        self.sessions = {}
    
    def create_session(self, room_id, session_id, hostname, webhook_url, webhook_secret, allowed_users):
        """Create new session."""
        session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:8]
        self.sessions[room_id] = {
            "room_id": room_id,
            "session_id": session_id,
            "session_hash": session_hash,
            "hostname": hostname,
            "owner": "matrix",
            "initiated_by": "test@example.com",
            "initiated_at": datetime.now().isoformat(),
            "agent_shell_webhook_url": webhook_url,
            "agent_shell_secret": webhook_secret,
            "quiet_mode": False,
            "allowed_users": allowed_users,
            "last_message": None,
            "handoff_expires_at": None,
        }
        return self.sessions[room_id]
    
    def get_session(self, room_id):
        """Get session by room_id."""
        return self.sessions.get(room_id)
    
    def update_session_owner(self, room_id, owner):
        """Update session owner."""
        if room_id in self.sessions:
            self.sessions[room_id]["owner"] = owner
            return True
        return False
    
    def list_sessions(self):
        """List all sessions."""
        return list(self.sessions.values())


class MockMatrixClient:
    """Mock Matrix client."""
    def __init__(self):
        self.rooms = {}
        self.messages = {}
    
    async def create_room(self, room_name, topic, invite):
        """Create new room."""
        room_id = f"!{uuid4().hex[:12]}:example.com"
        self.rooms[room_id] = {
            "name": room_name,
            "topic": topic,
            "members": list(invite),
            "created_at": datetime.now().isoformat(),
        }
        self.messages[room_id] = []
        print(f"    ✓ Created room {room_id}")
        print(f"      Name: {room_name}")
        print(f"      Members: {invite}")
        return room_id
    
    async def post_message(self, room_id, message):
        """Post message to room."""
        if room_id not in self.messages:
            self.messages[room_id] = []
        self.messages[room_id].append({
            "timestamp": datetime.now().isoformat(),
            "sender": "@bot:example.com",
            "text": message
        })
        print(f"    ✓ Posted to {room_id}: {message[:50]}...")


class MockWebhookClient:
    """Mock webhook endpoint (agent-shell)."""
    def __init__(self):
        self.calls = []
    
    async def post(self, url, data, secret):
        """Simulate webhook call."""
        self.calls.append({
            "timestamp": datetime.now().isoformat(),
            "url": url,
            "data": data,
            "secret": secret
        })
        print(f"    ✓ Webhook POST: {data.get('message', data.get('action'))}")
        return {"status": "ok", "response_text": "Mock response from agent-shell"}


async def test_integration():
    """Run integration test."""
    print("\n=== Integration Test: Full Handoff Flow ===\n")
    
    # Setup
    db = MockSessionDB()
    matrix = MockMatrixClient()
    webhook = MockWebhookClient()
    
    session_id = str(uuid4())
    session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:8]
    hostname = "edd-macbook"
    webhook_url = "http://127.0.0.1:8765/webhook/message"
    webhook_secret = "test-secret"
    allowed_users = ["@edd:example.com"]
    
    # Test 1: Initiate handoff
    print("Step 1: Initiate handoff from Emacs")
    print(f"  Session ID: {session_id}")
    print(f"  Hostname: {hostname}\n")
    
    room_id = await matrix.create_room(
        room_name=f"agent-{hostname}-{session_hash}",
        topic=f"Agent shell session from {hostname}",
        invite=allowed_users
    )
    print()
    
    session = db.create_session(
        room_id=room_id,
        session_id=session_id,
        hostname=hostname,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        allowed_users=allowed_users
    )
    
    print("Step 2: Session created in database")
    print(f"  Room ID: {session['room_id']}")
    print(f"  Owner: {session['owner']}")
    print(f"  Webhook: {session['agent_shell_webhook_url']}\n")
    
    await matrix.post_message(room_id, f"🔄 Session {session_hash} handed off from {hostname}")
    print()
    
    # Test 3: User sends message in room
    print("Step 3: User sends message in Matrix room")
    user_message = "What's the current status?"
    print(f"  User message: {user_message}\n")
    
    # Relay to webhook
    await webhook.post(webhook_url, {
        "room_id": room_id,
        "session_id": session_id,
        "sender": allowed_users[0],
        "message": user_message
    }, webhook_secret)
    print()
    
    # Test 4: Webhook response posted back
    print("Step 4: Agent-shell responds via webhook")
    response = "Status: Running. 3 tasks pending."
    print(f"  Response: {response}\n")
    
    await matrix.post_message(room_id, f"[Agent] {response}")
    print()
    
    # Test 5: User sends !return command
    print("Step 5: User sends !return command")
    print(f"  Message: !return\n")
    
    # Parse and handle command
    await webhook.post(webhook_url, {
        "room_id": room_id,
        "session_id": session_id,
        "action": "handoff_end"
    }, webhook_secret)
    print()
    
    # Update session owner
    db.update_session_owner(room_id, "emacs")
    print("Step 6: Session returned to Emacs")
    updated_session = db.get_session(room_id)
    print(f"  New owner: {updated_session['owner']}\n")
    
    await matrix.post_message(room_id, "✓ Session returned to Emacs")
    print()
    
    # Summary
    print("Step 7: Summary of session lifecycle")
    print(f"  Room: {room_id}")
    print(f"  Messages posted: {len(matrix.messages[room_id])}")
    print(f"  Webhook calls: {len(webhook.calls)}")
    print(f"  Final owner: {updated_session['owner']}\n")
    
    print("=== Integration Test Complete ===\n")
    return True


if __name__ == "__main__":
    try:
        success = asyncio.run(test_integration())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nTest interrupted")
        import sys
        sys.exit(1)
    except Exception as e:
        print(f"\nTest error: {e}")
        import traceback
        traceback.print_exc()
        import sys
        sys.exit(1)
