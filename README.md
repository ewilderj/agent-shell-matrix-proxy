# Matrix Proxy Bot

A minimal Matrix bot that relays messages between Matrix and local agent-shell sessions via webhooks, with **optional E2E encryption and user verification**.

**Purpose:** Enable portable agent-shell sessions that can be accessed from Matrix (phone/web) and handed off to Emacs when you're at your desk.

## Features

- 🔐 **E2E encryption** (optional) — All messages encrypted end-to-end
- ✅ **User verification** (optional) — SAS verification with cross-signing (green shield in Element)
- 🔄 **Message relay** between Matrix rooms and webhook clients
- 🤝 **Handoff logic** to switch session ownership (Matrix ↔ Emacs)
- 💾 **Session tracking** (SQLite)
- 🔐 **Webhook authentication** (secret key)
- 🔇 **Quiet mode** (optional: don't echo responses back to Matrix)

## Setup

### Prerequisites

- Python 3.13+
- A Matrix homeserver with a bot account

### Quick Install (Plain-Text Mode)

Works without any build tools:

```bash
git clone https://github.com/yourusername/matrix-proxy-bot ~/git/matrix-proxy-bot
cd ~/git/matrix-proxy-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# Edit .env with your Matrix credentials
python -m matrix_proxy_bot
```

The bot will run in **plain-text mode** (no encryption). Perfect for testing and private homeservers.

### Optional: Enable E2E Encryption

To add E2E encryption and verification, install build tools first:

**macOS:**
```bash
brew install libolm cmake autoconf automake libtool
pip install -e '.[e2e]'
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get install libolm-dev cmake build-essential libssl-dev
pip install -e '.[e2e]'
```

Then set `MATRIX_BOT_PASSWORD` in `.env` before first run. The bot will bootstrap cross-signing keys automatically.

## First Run

```bash
# Set MATRIX_BOT_PASSWORD in .env, then:
python -m matrix_proxy_bot

# On first run, bot prints:
#   Access token: syt_xxx...
#   Device ID: ABCD123

# Save these to .env, remove password, restart
```

## User Verification

If E2E is enabled:

1. In your Matrix client, open the bot's profile
2. Tap **Verify** or send `verify` in a DM
3. Bot displays emoji challenge
4. Bot auto-confirms (green shield appears)

## Usage

### Connecting from agent-shell (Emacs)

(Documentation pending — will be in agent-shell-matrix-remote.el)

### Webhook API

Send messages to the webhook:

```bash
curl -X POST http://127.0.0.1:8765/webhook/message \
  -H "Authorization: Bearer your-webhook-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "room_id": "!roomid:eddpod.com",
    "message": "Hello from agent-shell!"
  }'
```

Perform handoffs:

```bash
curl -X POST http://127.0.0.1:8765/handoff \
  -H "Authorization: Bearer your-webhook-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "room_id": "!roomid:eddpod.com",
    "owner": "emacs"
  }'
```

Query session status:

```bash
curl -X GET http://127.0.0.1:8765/session/!roomid:eddpod.com \
  -H "Authorization: Bearer your-webhook-secret"
```

## Architecture

```
Matrix Homeserver
    ↓ (Matrix protocol)
Matrix Proxy Bot (matrix-nio, optional E2E)
    ↓ (HTTP/WebSocket)
    ├─ POST /webhook/message (from agent-shell)
    ├─ POST /handoff (switch owners)
    └─ GET /session/{id} (status)
```

## Database

Sessions table: `room_id` ↔ `session_id` ↔ `owner` (matrix|emacs) ↔ `last_message`

No conversation history stored (stateless relay).

## Development

```bash
# Run tests
pytest -v

# Format
ruff format src/

# Lint
ruff check src/
```

## License

MIT
