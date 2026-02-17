# Curl Tests for Matrix Proxy Bot

Manual testing of handoff endpoints without requiring agent-shell or automated test framework.

## Prerequisites

1. **Bot running:**
   ```bash
   cd ~/git/matrix-proxy-bot
   uv run matrix_proxy_bot
   ```

2. **Bot account created** and logged in (see QUICK_START.md)

3. **Webhook server ready** (optional, for testing full relay):
   ```bash
   # In another terminal, start mock webhook server
   python3 tests/test_handoff_flow.py
   ```

## Quick Start

Run all tests at once:

```bash
bash tests/curl_tests.sh
```

The script will:
1. ✓ Create handoff session
2. ✓ Query session status
3. ✓ Send message responses
4. ✓ Send formatted responses
5. ✓ Send command responses
6. ✓ List active sessions
7. ✓ Test bad auth (expect 401)
8. ✓ Test missing auth (expect 401)

## Manual Tests (Step-by-Step)

### Setup

```bash
export BOT_URL="http://127.0.0.1:8765"
export WEBHOOK_SECRET=$(grep WEBHOOK_SECRET ~/.emacs.d/.env | cut -d= -f2)
export AUTH="Authorization: Bearer $WEBHOOK_SECRET"
```

### Test 1: Initiate Handoff

Create a new session:

```bash
curl -X POST "$BOT_URL/handoff" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "my-session-001",
    "hostname": "my-laptop",
    "webhook_url": "http://127.0.0.1:8888/webhook/message",
    "webhook_secret": "my-agent-secret",
    "message": "Test handoff",
    "quiet_mode": false,
    "ttl_seconds": 3600
  }' | jq .
```

**Expected response (200 OK):**
```json
{
  "status": "handoff_started",
  "room_id": "!abc123:eddpod.com",
  "room_url": "https://element.io/#/room/!abc123:eddpod.com",
  "session_id": "my-session-001",
  "session_hash": "a3f8c2d1"
}
```

**Save the room_id** for remaining tests:
```bash
export ROOM_ID="!abc123:eddpod.com"
export SESSION_ID="my-session-001"
```

### Test 2: Query Session Status

```bash
curl -X GET "$BOT_URL/session/$ROOM_ID" \
  -H "$AUTH" | jq .
```

**Expected response (200 OK):**
```json
{
  "room_id": "!abc123:eddpod.com",
  "session_id": "my-session-001",
  "session_hash": "a3f8c2d1",
  "hostname": "my-laptop",
  "owner": "matrix",
  "initiated_by": "@copilot:eddpod.com",
  "initiated_at": "2026-02-17T03:00:00Z",
  "webhook_url": "http://127.0.0.1:8888/webhook/message",
  "quiet_mode": false,
  "last_message": "2026-02-17T03:00:00Z",
  "handoff_expires_at": "2026-02-17T04:00:00Z"
}
```

### Test 3: Send Plain Text Response

Agent-shell sends a response:

```bash
curl -X POST "$BOT_URL/webhook/message" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d "{
    \"room_id\": \"$ROOM_ID\",
    \"session_id\": \"$SESSION_ID\",
    \"response_text\": \"5 tasks running\"
  }" | jq .
```

**Expected response (200 OK):**
```json
{
  "status": "message_posted",
  "room_id": "!abc123:eddpod.com"
}
```

**Expected Matrix room message:**
```
[Agent] 5 tasks running
```

### Test 4: Send Formatted Response

Agent-shell sends markdown-formatted response:

```bash
curl -X POST "$BOT_URL/webhook/message" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d "{
    \"room_id\": \"$ROOM_ID\",
    \"session_id\": \"$SESSION_ID\",
    \"response_text\": \"Status: Running (5 tasks)\",
    \"format\": \"markdown\",
    \"formatted_body\": \"**Status:** Running (5 tasks)\"
  }" | jq .
```

**Expected response (200 OK):**
```json
{
  "status": "message_posted",
  "room_id": "!abc123:eddpod.com"
}
```

**Expected Matrix room message:**
```
**Status:** Running (5 tasks)
```
(Rendered as bold in Matrix)

### Test 5: Send Command Response

Agent-shell responds to `!return` command:

```bash
curl -X POST "$BOT_URL/webhook/message" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d "{
    \"room_id\": \"$ROOM_ID\",
    \"session_id\": \"$SESSION_ID\",
    \"action\": \"handoff_end\",
    \"status\": \"success\"
  }" | jq .
```

**Expected response (200 OK):**
```json
{
  "status": "message_posted",
  "room_id": "!abc123:eddpod.com"
}
```

**Expected Matrix room message:**
```
✓ Session returned to Emacs
```

**Expected DB change:** `owner` changes from "matrix" to "emacs"

### Test 6: List Active Sessions

```bash
curl -X GET "$BOT_URL/sessions" \
  -H "$AUTH" | jq .
```

**Expected response (200 OK):**
```json
{
  "sessions": [
    {
      "room_id": "!abc123:eddpod.com",
      "hostname": "my-laptop",
      "owner": "emacs",
      "initiated_at": "2026-02-17T03:00:00Z"
    }
  ],
  "total": 1
}
```

### Test 7: Bad Authentication

```bash
curl -X POST "$BOT_URL/handoff" \
  -H "Authorization: Bearer wrong-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test",
    "hostname": "test",
    "webhook_url": "http://127.0.0.1:8888/webhook",
    "webhook_secret": "test"
  }' | jq .
```

**Expected response (401 Unauthorized):**
```json
{
  "detail": "Unauthorized"
}
```

### Test 8: Missing Authentication

```bash
curl -X GET "$BOT_URL/sessions" | jq .
```

**Expected response (401 Unauthorized):**
```json
{
  "detail": "Unauthorized"
}
```

### Test 9: Session Not Found

```bash
curl -X GET "$BOT_URL/session/!nonexistent:example.com" \
  -H "$AUTH" | jq .
```

**Expected response (404 Not Found):**
```json
{
  "detail": "Session not found"
}
```

## Testing with Element.io

Once a session is created, you can join the room in Element and:

1. **Send messages** — Bot should relay to webhook
2. **Send !return** — Should return session to Emacs
3. **Send !status** — Should show session info
4. **Send !help** — Should show available commands
5. **Send !close** — Should archive session

## Debugging

### Check bot logs

```bash
# Bot running in terminal, see logs in real-time
uv run matrix_proxy_bot
```

### Query database directly

```bash
# Check sessions table
sqlite3 ~/.matrix-proxy-bot/sessions.db "SELECT * FROM sessions;"
```

### Check Matrix room

1. Open Element.io
2. Navigate to the room (copy room_id from /handoff response)
3. Watch messages appear as you test

## Troubleshooting

| Issue | Solution |
|-------|----------|
| 401 Unauthorized | Check WEBHOOK_SECRET in .env |
| 404 Session not found | Use correct room_id from /handoff response |
| Webhook timeout | Ensure mock webhook server is running (if testing) |
| Room not created | Check bot has permission to create rooms on homeserver |
| Messages not appearing | Check owner is "matrix" (not "emacs") |

## Next Steps

Once curl tests pass:
1. Connect actual agent-shell
2. Test real message relay
3. Test real command execution
4. Monitor performance with concurrent sessions
