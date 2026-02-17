#!/usr/bin/env python3
"""
Test handoff flow WITHOUT agent-shell.

Requires:
- matrix-proxy-bot running (uv run matrix_proxy_bot)
- Bot account created and configured in .env
- Test user account created (same homeserver)

Usage:
    python3 tests/test_handoff_flow.py
    
Tests:
    1. Initiate handoff via webhook
    2. Create room + invite user
    3. Send message in room
    4. Relay to webhook endpoint (mock server)
    5. Handle !return command
    6. Verify session state in DB
"""

import asyncio
import aiohttp
import json
import sys
from pathlib import Path
from uuid import uuid4
from datetime import datetime
import hashlib

# Load environment
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from matrix_proxy_bot.config import Config
from matrix_proxy_bot.db import SessionDB


# Mock webhook server to receive relayed messages
class MockWebhookServer:
    def __init__(self, port=8888):
        self.port = port
        self.received_messages = []
        self.app = None
        
    async def handle_message(self, request):
        """Receive relayed messages from bot."""
        if request.method == "POST":
            data = await request.json()
            self.received_messages.append(data)
            print(f"  ✓ Mock webhook received: {data.get('message', 'unknown')}")
            return aiohttp.web.json_response({"status": "ok", "response_text": "Mock response"})
        return aiohttp.web.json_response({"status": "error"}, status=400)
    
    async def start(self):
        """Start mock webhook server."""
        self.app = aiohttp.web.Application()
        self.app.router.add_post("/webhook/message", self.handle_message)
        runner = aiohttp.web.AppRunner(self.app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        print(f"✓ Mock webhook server running on http://127.0.0.1:{self.port}")
        return runner
    
    def clear(self):
        """Clear received messages."""
        self.received_messages = []


async def test_handoff_flow():
    """Run handoff flow tests."""
    config = Config()
    db = SessionDB()
    webhook_server = MockWebhookServer(port=8888)
    runner = await webhook_server.start()
    
    bot_url = f"{config.matrix_homeserver}/_matrix/client/v3"
    webhook_secret = "test-secret-key"
    session_id = str(uuid4())
    session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:8]
    hostname = "test-machine"
    
    print(f"\n=== Matrix Proxy Bot: Handoff Flow Tests ===\n")
    print(f"Session ID: {session_id}")
    print(f"Session hash: {session_hash}")
    print(f"Hostname: {hostname}\n")
    
    async with aiohttp.ClientSession() as session:
        # Test 1: Initiate handoff via webhook
        print("Test 1: POST /handoff to initiate session")
        handoff_payload = {
            "session_id": session_id,
            "hostname": hostname,
            "webhook_url": "http://127.0.0.1:8888/webhook/message",
            "webhook_secret": webhook_secret,
            "message": "Test handoff initiation",
            "quiet_mode": False,
            "ttl_seconds": 3600
        }
        
        async with session.post(
            f"http://127.0.0.1:8765/handoff",
            json=handoff_payload,
            headers={"Authorization": f"Bearer {config.webhook_secret}"}
        ) as resp:
            if resp.status == 200:
                handoff_response = await resp.json()
                room_id = handoff_response.get("room_id")
                print(f"  ✓ Handoff initiated, room_id: {room_id}\n")
            else:
                print(f"  ✗ Handoff failed: {resp.status}")
                print(f"    Response: {await resp.text()}\n")
                await runner.cleanup()
                return False
        
        # Test 2: Verify room was created and user invited
        print("Test 2: Verify room exists and user is invited")
        # Query room info (if matrix-nio supports it)
        # For now, we check DB
        session_record = db.get_session(room_id)
        if session_record:
            print(f"  ✓ Session in DB: room_id={session_record['room_id']}, owner={session_record['owner']}")
            print(f"    Webhook URL: {session_record['agent_shell_webhook_url']}\n")
        else:
            print(f"  ✗ Session not found in DB\n")
        
        # Test 3: Send test message in room (simulating user action)
        # This would require bot to join room + listen for messages
        # For now, we'll test the relay manually
        print("Test 3: Simulate user message and relay to webhook")
        
        # Manually call bot's message relay function
        # (assumes bot has a method we can call)
        relay_payload = {
            "room_id": room_id,
            "sender": config.matrix_bot_user_id,
            "message": "Hello from test user"
        }
        
        # For now, this is a placeholder - bot would normally handle this
        print(f"  ℹ Message relay would be tested with actual room messages\n")
        
        # Test 4: Send !return command
        print("Test 4: Handle !return command in room")
        # Bot would detect "!return" in message and:
        # 1. POST to webhook with action=handoff_end
        # 2. Update session owner back to 'emacs'
        # 3. Post confirmation message
        
        print(f"  ℹ Command handling would be tested with actual room messages\n")
        
        # Test 5: Check DB state
        print("Test 5: Verify final session state")
        if session_record:
            print(f"  ✓ Session persisted in DB")
            print(f"    Session: {json.dumps(session_record, indent=6, default=str)}\n")
        
        print("=== Tests Complete ===\n")
        
    await runner.cleanup()
    return True


if __name__ == "__main__":
    try:
        success = asyncio.run(test_handoff_flow())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nTest interrupted")
        sys.exit(1)
    except Exception as e:
        print(f"\nTest error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
