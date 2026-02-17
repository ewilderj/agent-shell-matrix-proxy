# Quick Start

## 1. Set up bot account

On your Matrix homeserver, create a bot account (e.g., `@proxy:eddpod.com`). Note the credentials.

## 2. Configure

```bash
cp .env.example .env
# Edit .env with:
# - MATRIX_HOMESERVER (your homeserver URL)
# - MATRIX_BOT_USER_ID (e.g., @proxy:eddpod.com)
# - MATRIX_BOT_PASSWORD (initial login only)
# - WEBHOOK_SECRET (any string, use same in agent-shell config)
```

## 3. First run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

python -m matrix_proxy_bot
```

On first run, bot prints access token and device ID. Add these to `.env`:

```
MATRIX_ACCESS_TOKEN=syt_xxx...
MATRIX_DEVICE_ID=ABCD123
```

Remove `MATRIX_BOT_PASSWORD`, run again without it.

## 4. Test webhook

In another terminal:

```bash
curl -X POST http://127.0.0.1:8765/webhook/message \
  -H "Authorization: Bearer your-webhook-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "room_id": "!roomid:eddpod.com",
    "message": "Hello from agent-shell!"
  }'
```

Should print to your Matrix room.

## 5. Connect agent-shell

(Documentation pending — need to create Elisp bridge in agent-shell)

For now, the Matrix side is working. You can:
- Send messages via webhook POST
- Perform handoffs via `/handoff` endpoint
- Query session status via `/session/{room_id}`

---

## Architecture So Far

```
┌─────────────────────────────────┐
│   Matrix Homeserver             │
│   (your Matrix chat)            │
└────────────────┬────────────────┘
                 │ Matrix protocol
                 ↓
┌─────────────────────────────────┐
│   matrix-proxy-bot (running)    │
│   - Listens for messages        │
│   - Tracks sessions (SQLite)    │
│   - FastAPI webhook server      │
└────┬────────────────────────┬───┘
     │ Webhook POST           │ Webhook RESPONSE
     ↓                        ↓
  agent-shell             (to agent-shell)
  (Elisp client)
  (TBD)
```

## Next: Agent-Shell Client

Once this bot is stable, we'll create:
- `agent-shell-matrix-remote.el` — Elisp client to connect to this bot
- Handoff commands in agent-shell
- Session management (load/save)

