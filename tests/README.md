# Tests

Tests for Matrix Proxy Bot handoff flow, without requiring a running Matrix server or agent-shell.

## Quick Start

```bash
# Test command parsing (!return, !close, !status, etc.)
python3 tests/test_command_parsing.py

# Test full handoff flow with mock Matrix client
python3 tests/test_integration.py

# Test webhook API endpoints (requires bot running)
# First: uv run matrix_proxy_bot
# Then: python3 tests/test_webhook_api.py

# Test handoff flow with real bot (requires bot running)
# First: uv run matrix_proxy_bot
# Then: python3 tests/test_handoff_flow.py
```

## Test Files

### `test_command_parsing.py`
Tests command recognition and parsing from user messages.

**Commands:**
- `!return` — Hand session back to Emacs
- `!close` — Archive and close session
- `!status` — Show session status
- `!help` — Show available commands

**Tests:**
- Basic command recognition
- Whitespace handling (leading, trailing, multiple)
- Arguments parsing
- Unknown command handling

**Run:** `python3 tests/test_command_parsing.py` (no dependencies)

---

### `test_integration.py`
Full handoff flow simulation using mock Matrix client and webhook server.

**Flow tested:**
1. Emacs initiates handoff → Room created
2. User joins room → Invited by bot
3. User sends message → Relayed to webhook
4. Agent-shell responds → Posted back to room
5. User sends `!return` → Session returned to Emacs
6. Session archived

**Run:** `python3 tests/test_integration.py` (no dependencies)

---

### `test_webhook_api.py`
Tests bot's HTTP webhook endpoints.

**Endpoints tested:**
- `POST /handoff` — Initiate session
- `POST /webhook/message` — Relay from agent-shell
- `GET /session/{room_id}` — Query session status

**Prerequisites:**
- Bot running: `uv run matrix_proxy_bot`
- Bot listening on `http://127.0.0.1:8765`

**Run:** `python3 tests/test_webhook_api.py`

---

### `test_handoff_flow.py`
Tests full handoff flow with real bot and mock webhook server.

**Flow tested:**
1. Bot receives handoff request via `/handoff` endpoint
2. Room created and user invited
3. Messages relayed to mock webhook server
4. Session tracked in database

**Prerequisites:**
- Bot running: `uv run matrix_proxy_bot`
- Test user account on Matrix server
- Bot invited to rooms (or auto-join enabled)

**Run:** `python3 tests/test_handoff_flow.py`

---

## Test Coverage

| Feature | Unit | Integration | E2E |
|---------|------|-------------|-----|
| Command parsing | ✓ | — | — |
| Room creation | — | ✓ | ✓ |
| Message relay | — | ✓ | ✓ |
| Webhook auth | — | ✓ | ✓ |
| Session DB | ✓ | ✓ | ✓ |
| !return command | ✓ | ✓ | (pending) |
| TTL/auto-return | — | — | (pending) |

## Next Steps

Once handoff endpoints are implemented:
1. Run `test_integration.py` to verify flow
2. Run `test_webhook_api.py` with bot running
3. Add real Matrix client tests
4. Test with actual agent-shell integration
