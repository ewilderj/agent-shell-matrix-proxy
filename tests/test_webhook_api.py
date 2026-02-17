#!/usr/bin/env python3
"""
Test webhook endpoints without Matrix.

Tests bot's HTTP interface:
- POST /handoff (initiate session)
- POST /webhook/message (relay from agent-shell)
- GET /session/{room_id} (query status)
- Message parsing (!return, !close, etc.)

Usage:
    python3 tests/test_webhook_api.py
"""

import asyncio
import aiohttp
import json
import sys
from pathlib import Path
from uuid import uuid4
import hashlib

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from matrix_proxy_bot.config import Config


async def test_webhook_api():
    """Test webhook API endpoints."""
    config = Config()
    bot_url = "http://127.0.0.1:8765"
    
    print("\n=== Matrix Proxy Bot: Webhook API Tests ===\n")
    print(f"Bot webhook URL: {bot_url}")
    print(f"Webhook secret (from config): {config.webhook_secret[:20]}...\n")
    
    async with aiohttp.ClientSession() as session:
        
        # Test 1: POST /handoff with auth
        print("Test 1: POST /handoff with Bearer token auth")
        session_id = str(uuid4())
        handoff_payload = {
            "session_id": session_id,
            "hostname": "test-laptop",
            "webhook_url": "http://127.0.0.1:8888/webhook/message",
            "webhook_secret": "agent-shell-secret",
            "message": "Starting handoff",
            "quiet_mode": False,
            "ttl_seconds": 3600
        }
        
        try:
            async with session.post(
                f"{bot_url}/handoff",
                json=handoff_payload,
                headers={"Authorization": f"Bearer {config.webhook_secret}"}
            ) as resp:
                print(f"  Status: {resp.status}")
                body = await resp.json()
                print(f"  Response: {json.dumps(body, indent=4, default=str)}")
                if resp.status == 200:
                    room_id = body.get("room_id")
                    print(f"  ✓ Handoff successful\n")
                else:
                    print(f"  ✗ Handoff failed\n")
        except Exception as e:
            print(f"  ✗ Error: {e}\n")
        
        # Test 2: POST /webhook/message (agent-shell → bot → matrix)
        print("Test 2: POST /webhook/message (relay from agent-shell)")
        message_payload = {
            "room_id": "!test123:example.com",
            "session_id": session_id,
            "message": "Response from agent-shell"
        }
        
        try:
            async with session.post(
                f"{bot_url}/webhook/message",
                json=message_payload,
                headers={"Authorization": f"Bearer {config.webhook_secret}"}
            ) as resp:
                print(f"  Status: {resp.status}")
                if resp.status == 200:
                    print(f"  ✓ Message relay successful\n")
                else:
                    print(f"  ✗ Message relay failed")
                    print(f"    {await resp.text()}\n")
        except Exception as e:
            print(f"  ✗ Error: {e}\n")
        
        # Test 3: Bad auth
        print("Test 3: POST /handoff with bad auth (should fail)")
        try:
            async with session.post(
                f"{bot_url}/handoff",
                json=handoff_payload,
                headers={"Authorization": "Bearer wrong-secret"}
            ) as resp:
                print(f"  Status: {resp.status}")
                if resp.status == 401:
                    print(f"  ✓ Correctly rejected\n")
                else:
                    print(f"  ✗ Should have been 401\n")
        except Exception as e:
            print(f"  ✗ Error: {e}\n")
        
        # Test 4: GET /session/{room_id}
        print("Test 4: GET /session/{room_id} (query status)")
        if 'room_id' in locals():
            try:
                async with session.get(
                    f"{bot_url}/session/{room_id}",
                    headers={"Authorization": f"Bearer {config.webhook_secret}"}
                ) as resp:
                    print(f"  Status: {resp.status}")
                    if resp.status == 200:
                        body = await resp.json()
                        print(f"  Session: {json.dumps(body, indent=4, default=str)}")
                        print(f"  ✓ Query successful\n")
                    else:
                        print(f"  ✗ Query failed: {await resp.text()}\n")
            except Exception as e:
                print(f"  ✗ Error: {e}\n")
        
        print("=== Tests Complete ===\n")


if __name__ == "__main__":
    try:
        asyncio.run(test_webhook_api())
    except KeyboardInterrupt:
        print("\nTest interrupted")
        sys.exit(1)
    except Exception as e:
        print(f"\nTest error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
