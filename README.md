# Matrix Proxy Bot

A minimal Matrix bot that relays messages between Matrix and local agent-shell sessions via webhooks.

**Purpose:** Enable portable agent-shell sessions that can be accessed from Matrix (phone/web) and handed off to Emacs when you're at your desk.

## Features

- 🔄 **Message relay** between Matrix rooms and webhook clients
- 🤝 **Handoff logic** to switch session ownership (Matrix ↔ Emacs)
- 🔒 **E2E encryption** support (matrix-nio)
- 💾 **Session tracking** (SQLite)
- 🔐 **Webhook authentication** (secret key)
- 🔇 **Quiet mode** (optional: don't echo responses back to Matrix)

## Setup

### Prerequisites

- Python 3.13+
- `uv` (or `pip`)
- A Matrix homeserver with a bot account

### Install

```bash
git clone https://github.com/yourusername/matrix-proxy-bot ~/git/matrix-proxy-bot
cd ~/git/matrix-proxy-bot
cp .env.example .env
# Edit .env with your Matrix credentials and webhook config
```

### First Run

Set `MATRIX_BOT_PASSWORD` in `.env`, then:

```bash
uv run matrix_proxy_bot
```

The bot will log in, print an access token and device ID. Save these to `.env`:

```bash
MATRIX_ACCESS_TOKEN=syt_xxx...
MATRIX_DEVICE_ID=ABCD123...
```

Then remove the password and run again:

```bash
uv run matrix_proxy_bot
```

## Usage

### As a daemon

```bash
# Run in foreground with debug logging
MATRIX_LOG_LEVEL=DEBUG uv run matrix_proxy_bot

# Or integrate with systemd (see scripts/matrix-proxy-bot.service)
```

### Agent-Shell Connection

From agent-shell (Elisp), connect to the webhook endpoint:

```elisp
(agent-shell-connect-webhook "http://127.0.0.1:8765" "your-secret-key")
```

Send a message from agent-shell → bot receives it → forwards to Matrix room → response sent back to agent-shell.

### Handoff API

**Handoff to Emacs:**
```bash
curl -X POST http://127.0.0.1:8765/handoff \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "room_xyz", "owner": "emacs"}'
```

**Handoff to Matrix:**
```bash
curl -X POST http://127.0.0.1:8765/handoff \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "room_xyz", "owner": "matrix"}'
```

## Architecture

```
Matrix Homeserver
    ↓ (Matrix client protocol)
Matrix Proxy Bot (matrix-nio)
    ↓ (HTTP/WebSocket)
    ├─ POST /webhook/message (from agent-shell)
    ├─ POST /handoff (switch owners)
    └─ GET /session/{id} (status)
```

## Database Schema

Sessions table: `room_id` ↔ `session_id` ↔ `owner` (matrix|emacs) ↔ `last_message`

No conversation history stored (stateless relay). For persistence, that's agent-shell's job.

## Development

```bash
# Run tests
uv run pytest -v

# Format
uv run ruff format src/

# Lint
uv run ruff check src/
```

## License

MIT
