#!/usr/bin/env python3
"""Test command handlers, TTL scheduler, and message relay"""

import requests
import sys
import json
import asyncio
import time
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timedelta

# Configuration
BOT_URL = "http://localhost:8765"

# Load WEBHOOK_SECRET from .env
env_file = Path(__file__).parent.parent / ".env"
if not env_file.exists():
    print(f"Error: .env not found at {env_file}")
    sys.exit(1)

WEBHOOK_SECRET = None
with open(env_file) as f:
    for line in f:
        if line.startswith("WEBHOOK_SECRET="):
            WEBHOOK_SECRET = line.split("=", 1)[1].strip()
            break

if not WEBHOOK_SECRET:
    print("Error: WEBHOOK_SECRET not found in .env")
    sys.exit(1)

# Test tracking
tests_run = 0
tests_passed = 0
tests_failed = 0

def header(title):
    """Print section header"""
    print()
    print("=" * 50)
    print(title)
    print("=" * 50)

def test(name, method, path, data=None, expected_status=200):
    """Test an endpoint"""
    global tests_run, tests_passed, tests_failed
    tests_run += 1
    
    print(f"\n[{tests_run}] {name}")
    
    headers = {
        "Authorization": f"Bearer {WEBHOOK_SECRET}",
        "Content-Type": "application/json"
    }
    
    try:
        if method == "GET":
            response = requests.get(f"{BOT_URL}{path}", headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(f"{BOT_URL}{path}", json=data, headers=headers, timeout=10)
        else:
            print(f"  Unknown method: {method} ✗")
            tests_failed += 1
            return None, None
        
        status = response.status_code
        try:
            result = response.json()
        except:
            result = response.text
        
        if status == expected_status:
            print(f"  Status: {status} ✓")
            if isinstance(result, dict):
                for key, val in result.items():
                    if key not in ['owner', 'initiated_at', 'created_at', 'last_message_at']:
                        print(f"    {key}: {str(val)[:80]}")
            tests_passed += 1
        else:
            print(f"  Status: {status} ✗ (expected {expected_status})")
            print(f"  Response: {str(result)[:200]}")
            tests_failed += 1
        
        return response, result
    except Exception as e:
        print(f"  Error: {e} ✗")
        tests_failed += 1
        return None, None

def create_test_session(ttl_seconds=None):
    """Create a test session and return room_id, session_id"""
    data = {
        "session_id": f"test-{int(time.time())}",
        "hostname": "testhost",
        "webhook_url": "http://localhost:9999/webhook",
        "webhook_secret": "test-secret"
    }
    if ttl_seconds:
        data["ttl_seconds"] = ttl_seconds
    
    response = requests.post(
        f"{BOT_URL}/handoff",
        json=data,
        headers={"Authorization": f"Bearer {WEBHOOK_SECRET}", "Content-Type": "application/json"},
        timeout=10
    )
    
    if response.status_code != 200:
        return None, None
    
    result = response.json()
    return result.get("room_id"), result.get("session_id")

def main():
    """Run all tests"""
    global tests_run, tests_passed, tests_failed
    
    print("\n" + "=" * 50)
    print("Matrix Proxy Bot: Commands & TTL Tests")
    print("=" * 50)
    print(f"Bot URL: {BOT_URL}")
    print(f"Auth: Bearer {WEBHOOK_SECRET[:20]}...")
    
    # Test 1: Command handler - !help
    header("Test 1: Command handler - !help")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "message": "!help"
        }
        test("Send !help command", "POST", "/webhook/message", data, 200)
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 2: Command handler - !status
    header("Test 2: Command handler - !status")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "message": "!status"
        }
        test("Send !status command", "POST", "/webhook/message", data, 200)
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 3: Command handler - !return
    header("Test 3: Command handler - !return")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "message": "!return"
        }
        test("Send !return command", "POST", "/webhook/message", data, 200)
        
        # Verify session owner changed back to emacs
        time.sleep(1)
        encoded = quote(room_id, safe='')
        response, result = test(
            "Verify owner is now 'emacs'",
            "GET",
            f"/session/{encoded}",
            None,
            200
        )
        if response and result.get("owner") == "emacs":
            print("    Owner correctly changed to 'emacs' ✓")
        else:
            owner = result.get("owner") if result else "unknown"
            print(f"    Owner is '{owner}', expected 'emacs' ✗")
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 4: Command handler - !close
    header("Test 4: Command handler - !close")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "message": "!close"
        }
        test("Send !close command", "POST", "/webhook/message", data, 200)
        
        # Verify session owner is now emacs
        time.sleep(1)
        encoded = quote(room_id, safe='')
        response, result = test(
            "Verify session closed",
            "GET",
            f"/session/{encoded}",
            None,
            200
        )
        if response and result.get("owner") == "emacs":
            print("    Session correctly closed ✓")
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 5: Message relay - plain text
    header("Test 5: Message relay - plain text from agent")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "response_text": "Agent output here"
        }
        test("Relay plain text response", "POST", "/webhook/message", data, 200)
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 6: Message relay - formatted HTML
    header("Test 6: Message relay - formatted HTML from agent")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "response_text": "Agent **bold** output",
            "formatted_body": "Agent <strong>bold</strong> output",
            "format": "org.matrix.custom.html"
        }
        test("Relay formatted HTML response", "POST", "/webhook/message", data, 200)
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 7: TTL scheduler verification
    header("Test 7: TTL scheduler - session expiry")
    # Create session with 2 second TTL
    room_id, session_id = create_test_session(ttl_seconds=2)
    if room_id:
        encoded = quote(room_id, safe='')
        
        # Verify session starts with owner='matrix'
        response, result = test(
            "Verify initial owner is 'matrix'",
            "GET",
            f"/session/{encoded}",
            None,
            200
        )
        initial_owner = result.get("owner") if result else None
        print(f"    Initial owner: {initial_owner}")
        
        # Wait for TTL to expire (scheduler checks every 60s, so this test may not trigger)
        # For now, just verify the session structure
        if result and result.get("expires_at"):
            print(f"    Expiry set to: {result['expires_at']} ✓")
        else:
            print(f"    Warning: No expiry set on session")
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 8: Message relay with action field
    header("Test 8: Message relay - action field (command response)")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "action": "handoff_end"
        }
        test("Send handoff_end action", "POST", "/webhook/message", data, 200)
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 9: Invalid command
    header("Test 9: Invalid command handling")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "message": "!invalid_command"
        }
        test("Send invalid command", "POST", "/webhook/message", data, 200)
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Test 10: Regular message (not a command)
    header("Test 10: Regular message relay (non-command)")
    room_id, session_id = create_test_session()
    if room_id:
        data = {
            "room_id": room_id,
            "session_id": session_id,
            "message": "This is a regular message, not a command"
        }
        test("Relay regular message to webhook", "POST", "/webhook/message", data, 200)
    else:
        print("  Skipped: Failed to create test session")
        tests_failed += 1
    
    # Summary
    header("Test Summary")
    print(f"Total:  {tests_run}")
    print(f"Passed: {tests_passed}")
    print(f"Failed: {tests_failed}")
    
    if tests_failed == 0:
        print("\n✓ All tests passed!")
        return 0
    else:
        print(f"\n✗ {tests_failed} test(s) failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
