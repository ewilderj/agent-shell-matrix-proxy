#!/bin/bash
set -e

# Curl test suite for matrix-proxy-bot handoff endpoints
# 
# Prerequisites:
#   - Bot running: uv run matrix_proxy_bot
#   - Bot account created and logged in
#   - .env configured with WEBHOOK_SECRET
#
# Usage:
#   bash tests/curl_tests.sh

BOT_URL="http://127.0.0.1:8765"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(grep WEBHOOK_SECRET .env | cut -d= -f2)}"

if [ -z "$WEBHOOK_SECRET" ]; then
    echo "Error: WEBHOOK_SECRET not found in .env or env var"
    exit 1
fi

AUTH_HEADER="Authorization: Bearer $WEBHOOK_SECRET"
CONTENT_TYPE="Content-Type: application/json"

echo "=== Matrix Proxy Bot: Curl Tests ==="
echo "Bot URL: $BOT_URL"
echo "Auth: Bearer ${WEBHOOK_SECRET:0:20}..."
echo ""

# Test 1: POST /handoff - Initiate session
echo "Test 1: POST /handoff - Initiate session handoff"
RESPONSE=$(curl -s -X POST "$BOT_URL/handoff" \
  -H "$AUTH_HEADER" \
  -H "$CONTENT_TYPE" \
  -d '{
    "session_id": "test-session-001",
    "hostname": "test-laptop",
    "webhook_url": "http://127.0.0.1:8888/webhook/message",
    "webhook_secret": "test-agent-secret",
    "message": "Handoff initiated from curl test",
    "quiet_mode": false,
    "ttl_seconds": 3600
  }')

echo "Response:"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"

# Extract room_id for later tests
ROOM_ID=$(echo "$RESPONSE" | jq -r '.room_id' 2>/dev/null)
if [ -z "$ROOM_ID" ] || [ "$ROOM_ID" = "null" ]; then
    echo "Error: Failed to create handoff session"
    exit 1
fi

SESSION_ID=$(echo "$RESPONSE" | jq -r '.session_id' 2>/dev/null)
echo ""
echo "✓ Handoff created: room_id=$ROOM_ID, session_id=$SESSION_ID"
echo ""

# Wait a moment for room to stabilize
sleep 2

# Test 2: GET /session/{room_id} - Query status
echo "Test 2: GET /session/{room_id} - Query session status"
RESPONSE=$(curl -s -X GET "$BOT_URL/session/$ROOM_ID" \
  -H "$AUTH_HEADER")

echo "Response:"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
echo ""

# Test 3: POST /webhook/message - Send message response
echo "Test 3: POST /webhook/message - Agent-shell sends response"
RESPONSE=$(curl -s -X POST "$BOT_URL/webhook/message" \
  -H "$AUTH_HEADER" \
  -H "$CONTENT_TYPE" \
  -d "{
    \"room_id\": \"$ROOM_ID\",
    \"session_id\": \"$SESSION_ID\",
    \"response_text\": \"Agent-shell response: 5 tasks running\",
    \"format\": \"plain\"
  }")

echo "Response:"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
echo ""

# Test 4: POST /webhook/message with formatted_body
echo "Test 4: POST /webhook/message - With markdown formatting"
RESPONSE=$(curl -s -X POST "$BOT_URL/webhook/message" \
  -H "$AUTH_HEADER" \
  -H "$CONTENT_TYPE" \
  -d "{
    \"room_id\": \"$ROOM_ID\",
    \"session_id\": \"$SESSION_ID\",
    \"response_text\": \"Status: Running (5 tasks)\",
    \"format\": \"markdown\",
    \"formatted_body\": \"**Status:** Running (5 tasks)\"
  }")

echo "Response:"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
echo ""

# Test 5: POST /webhook/message with action (command response)
echo "Test 5: POST /webhook/message - Command response (handoff_end)"
RESPONSE=$(curl -s -X POST "$BOT_URL/webhook/message" \
  -H "$AUTH_HEADER" \
  -H "$CONTENT_TYPE" \
  -d "{
    \"room_id\": \"$ROOM_ID\",
    \"session_id\": \"$SESSION_ID\",
    \"action\": \"handoff_end\",
    \"status\": \"success\"
  }")

echo "Response:"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
echo ""

# Test 6: GET /sessions - List all sessions
echo "Test 6: GET /sessions - List active sessions"
RESPONSE=$(curl -s -X GET "$BOT_URL/sessions" \
  -H "$AUTH_HEADER")

echo "Response:"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
echo ""

# Test 7: Bad auth
echo "Test 7: POST /handoff with bad auth (should fail)"
RESPONSE=$(curl -s -X POST "$BOT_URL/handoff" \
  -H "Authorization: Bearer wrong-secret" \
  -H "$CONTENT_TYPE" \
  -d '{
    "session_id": "test",
    "hostname": "test",
    "webhook_url": "http://127.0.0.1:8888/webhook",
    "webhook_secret": "test"
  }')

echo "Response (expect 401):"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
echo ""

# Test 8: Missing auth
echo "Test 8: POST /handoff without auth (should fail)"
RESPONSE=$(curl -s -X POST "$BOT_URL/handoff" \
  -H "$CONTENT_TYPE" \
  -d '{
    "session_id": "test",
    "hostname": "test",
    "webhook_url": "http://127.0.0.1:8888/webhook",
    "webhook_secret": "test"
  }')

echo "Response (expect 401):"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"
echo ""

echo "=== All Tests Complete ==="
