#!/usr/bin/env python3
"""Comprehensive test suite for matrix-proxy-bot endpoints"""

import requests
import sys
import json
from pathlib import Path
from urllib.parse import quote

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
    
    url = BOT_URL + path
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unknown method: {method}")
        
        status = response.status_code
        print(f"  Status: {status} (expected {expected_status})", end="")
        
        if status == expected_status:
            print(" ✓")
            tests_passed += 1
        else:
            print(" ✗")
            tests_failed += 1
        
        try:
            result = response.json()
        except:
            result = response.text
        
        return response, result
    
    except Exception as e:
        print(f"  Error: {e} ✗")
        tests_failed += 1
        return None, None

def main():
    global tests_run, tests_passed, tests_failed
    
    print("=" * 50)
    print("Matrix Proxy Bot: Comprehensive Test Suite")
    print("=" * 50)
    print(f"Bot URL: {BOT_URL}")
    print(f"Auth: Bearer {WEBHOOK_SECRET[:20]}...")
    
    # Test 1: Handoff
    header("Test 1: POST /handoff - Initiate handoff")
    response, result = test(
        "Create handoff session",
        "POST",
        "/handoff",
        {
            "session_id": "test-flow-1",
            "hostname": "testhost",
            "webhook_url": "http://localhost:9999/webhook",
            "webhook_secret": "test-secret"
        },
        200
    )
    
    if not response or response.status_code != 200:
        print("✗ Handoff failed, skipping remaining tests")
        print(f"Response: {result}")
        sys.exit(1)
    
    room_id = result.get("room_id")
    session_id = result.get("session_id")
    
    if not room_id or not session_id:
        print(f"✗ Failed to extract room_id and session_id from response")
        print(f"Response: {result}")
        sys.exit(1)
    
    print(f"  Room ID: {room_id}")
    print(f"  Session ID: {session_id}")
    
    # Test 2: List sessions
    header("Test 2: GET /sessions - List all sessions")
    response, result = test(
        "List active sessions",
        "GET",
        "/sessions",
        None,
        200
    )
    
    if response and response.status_code == 200:
        sessions = result.get("sessions", [])
        print(f"  Found {len(sessions)} active sessions")
        if sessions:
            print(f"  First session: {sessions[0].get('hostname')}")
    
    # Test 3: Get single session
    header("Test 3: GET /session/{id} - Get session details")
    encoded_room = quote(room_id, safe='')
    response, result = test(
        "Get session details",
        "GET",
        f"/session/{encoded_room}",
        None,
        200
    )
    
    if response and response.status_code == 200:
        print(f"  Owner: {result.get('owner')}")
        print(f"  Hostname: {result.get('hostname')}")
        print(f"  Initiated at: {result.get('initiated_at')}")
    
    # Test 4: Webhook message relay
    header("Test 4: POST /webhook/message - Relay agent response")
    response, result = test(
        "Post agent response to room",
        "POST",
        "/webhook/message",
        {
            "room_id": room_id,
            "session_id": session_id,
            "response_text": "Response from agent"
        },
        200
    )
    
    # Test 5: Webhook message with formatted body
    header("Test 5: POST /webhook/message - With HTML formatted body")
    response, result = test(
        "Post formatted response",
        "POST",
        "/webhook/message",
        {
            "room_id": room_id,
            "session_id": session_id,
            "response_text": "Formatted",
            "format": "org.matrix.custom.html",
            "formatted_body": "<strong>Formatted</strong> response"
        },
        200
    )
    
    # Test 6: Auth validation - missing auth
    header("Test 6: Security - Missing auth header")
    try:
        response = requests.get(f"{BOT_URL}/sessions")
        status = response.status_code
        print(f"\n[{tests_run + 1}] Request without auth")
        tests_run += 1
        
        if status in [401, 403]:
            print(f"  Status: {status} ✓")
            tests_passed += 1
        else:
            print(f"  Status: {status} ✗ (expected 401 or 403)")
            tests_failed += 1
    except Exception as e:
        print(f"  Error: {e} ✗")
        tests_failed += 1
    
    # Test 7: Auth validation - invalid token
    header("Test 7: Security - Invalid auth token")
    try:
        headers = {"Authorization": "Bearer invalid_token"}
        response = requests.get(f"{BOT_URL}/sessions", headers=headers)
        status = response.status_code
        print(f"\n[{tests_run + 1}] Invalid token rejection")
        tests_run += 1
        
        if status == 401:
            print(f"  Status: {status} ✓")
            tests_passed += 1
        else:
            print(f"  Status: {status} ✗ (expected 401)")
            tests_failed += 1
    except Exception as e:
        print(f"  Error: {e} ✗")
        tests_failed += 1
    
    # Test 8: Non-existent session
    header("Test 8: Error handling - Non-existent session")
    fake_room = "!nonexistent:eddpod.com"
    encoded_fake = quote(fake_room, safe='')
    response, result = test(
        "Non-existent session returns 404",
        "GET",
        f"/session/{encoded_fake}",
        None,
        404
    )
    
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
