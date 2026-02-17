#!/bin/bash
# Comprehensive test suite for matrix-proxy-bot
# Tests all endpoints and workflows without agent-shell

set +H  # Disable history expansion for ! characters
set -e  # Exit on error

# Configuration
BOT_URL="${BOT_URL:-http://localhost:8765}"
# Try multiple paths for .env
if [ -f .env ]; then
    WEBHOOK_SECRET=$(grep "^WEBHOOK_SECRET=" .env | cut -d= -f2)
elif [ -f ../.env ]; then
    WEBHOOK_SECRET=$(grep "^WEBHOOK_SECRET=" ../.env | cut -d= -f2)
else
    WEBHOOK_SECRET=$(grep "^WEBHOOK_SECRET=" ../../.env 2>/dev/null | cut -d= -f2 || true)
fi
if [ -z "$WEBHOOK_SECRET" ]; then
    echo "Error: WEBHOOK_SECRET not found in .env"
    exit 1
fi

# Test counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Helper functions
header() {
    echo ""
    echo "=========================================="
    echo "$1"
    echo "=========================================="
}

test_endpoint() {
    local name="$1"
    local method="$2"
    local path="$3"
    local data="$4"
    local expected_status="$5"
    
    TESTS_RUN=$((TESTS_RUN + 1))
    echo ""
    echo "[$TESTS_RUN] Testing: $name"
    
    if [ "$method" = "GET" ]; then
        response=$(curl -s -w "\n%{http_code}" -X GET "$BOT_URL$path" \
            -H "Authorization: Bearer $WEBHOOK_SECRET")
    else
        response=$(curl -s -w "\n%{http_code}" -X $method "$BOT_URL$path" \
            -H "Authorization: Bearer $WEBHOOK_SECRET" \
            -H "Content-Type: application/json" \
            --data-raw "$data")
    fi
    
    http_code=$(printf '%s\n' "$response" | tail -1)
    body=$(printf '%s\n' "$response" | sed '$d')
    
    echo "  Method: $method $path"
    echo "  Status: $http_code (expected $expected_status)"
    
    if [ "$http_code" = "$expected_status" ]; then
        echo "  ✓ PASS"
        echo "  Response: $(printf '%s\n' "$body" | cut -c1-100)..."
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo "$body"  # Return response for further processing
        return 0
    else
        echo "  ✗ FAIL"
        echo "  Response: $body"
        TESTS_FAILED=$((TESTS_FAILED + 1))
        return 1
    fi
}

# Test 1: Handoff initiation
header "Test 1: POST /handoff - Initiate handoff"
handoff_response=$(test_endpoint \
    "Create handoff session" \
    "POST" \
    "/handoff" \
    '{"session_id":"test-1","hostname":"testhost","webhook_url":"http://localhost:9000/webhook","webhook_secret":"secret"}' \
    "200" || echo '{"room_id":""}')

ROOM_ID=$(echo "$handoff_response" | python3 -c "import sys, json; print(json.load(sys.stdin).get('room_id', ''))" 2>/dev/null || echo "")
SESSION_ID=$(echo "$handoff_response" | python3 -c "import sys, json; print(json.load(sys.stdin).get('session_id', ''))" 2>/dev/null || echo "")

echo "Extracted: room_id=$ROOM_ID, session_id=$SESSION_ID"

# Test 2: List sessions
header "Test 2: GET /sessions - List all sessions"
test_endpoint \
    "List active sessions" \
    "GET" \
    "/sessions" \
    "" \
    "200" > /dev/null

# Test 3: Get single session
header "Test 3: GET /session/{id} - Get session details"
if [ -n "$ROOM_ID" ]; then
    ENCODED_ROOM=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$ROOM_ID'))" 2>/dev/null || echo "$ROOM_ID")
    test_endpoint \
        "Get session details" \
        "GET" \
        "/session/$ENCODED_ROOM" \
        "" \
        "200" > /dev/null
fi

# Test 4: Webhook message relay
header "Test 4: POST /webhook/message - Relay agent response"
if [ -n "$ROOM_ID" ] && [ -n "$SESSION_ID" ]; then
    test_endpoint \
        "Post agent response to room" \
        "POST" \
        "/webhook/message" \
        "{\"room_id\":\"$ROOM_ID\",\"session_id\":\"$SESSION_ID\",\"response_text\":\"Response from agent\"}" \
        "200" > /dev/null
fi

# Test 5: Webhook message with formatted body
header "Test 5: POST /webhook/message - With HTML formatted body"
if [ -n "$ROOM_ID" ] && [ -n "$SESSION_ID" ]; then
    test_endpoint \
        "Post formatted response" \
        "POST" \
        "/webhook/message" \
        "{\"room_id\":\"$ROOM_ID\",\"session_id\":\"$SESSION_ID\",\"response_text\":\"Formatted\",\"format\":\"org.matrix.custom.html\",\"formatted_body\":\"<strong>Formatted</strong> response\"}" \
        "200" > /dev/null
fi

# Test 6: Auth validation - missing auth header
header "Test 6: Security - Missing auth header"
response=$(curl -s -w "\n%{http_code}" -X GET "$BOT_URL/sessions")
http_code=$(printf '%s\n' "$response" | tail -1)
if [ "$http_code" = "403" ] || [ "$http_code" = "401" ]; then
    echo "✓ PASS - Request rejected without auth"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    echo "✗ FAIL - Request accepted without auth (status: $http_code)"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi
TESTS_RUN=$((TESTS_RUN + 1))

# Test 7: Auth validation - invalid token
header "Test 7: Security - Invalid auth token"
response=$(curl -s -w "\n%{http_code}" -X GET "$BOT_URL/sessions" \
    -H "Authorization: Bearer invalid_token")
http_code=$(printf '%s\n' "$response" | tail -1)
if [ "$http_code" = "401" ]; then
    echo "✓ PASS - Invalid token rejected"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    echo "✗ FAIL - Invalid token accepted (status: $http_code)"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi
TESTS_RUN=$((TESTS_RUN + 1))

# Test 8: Non-existent session
header "Test 8: Error handling - Non-existent session"
FAKE_ROOM="!nonexistent:eddpod.com"
ENCODED_FAKE=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$FAKE_ROOM'))" 2>/dev/null)
response=$(curl -s -w "\n%{http_code}" -X GET "$BOT_URL/session/$ENCODED_FAKE" \
    -H "Authorization: Bearer $WEBHOOK_SECRET")
http_code=$(printf '%s\n' "$response" | tail -1)
if [ "$http_code" = "404" ]; then
    echo "✓ PASS - Non-existent session returns 404"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    echo "✗ FAIL - Expected 404, got $http_code"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi
TESTS_RUN=$((TESTS_RUN + 1))

# Summary
header "Test Summary"
echo "Total:  $TESTS_RUN"
echo "Passed: $TESTS_PASSED"
echo "Failed: $TESTS_FAILED"

if [ $TESTS_FAILED -eq 0 ]; then
    echo ""
    echo "✓ All tests passed!"
    exit 0
else
    echo ""
    echo "✗ Some tests failed"
    exit 1
fi
