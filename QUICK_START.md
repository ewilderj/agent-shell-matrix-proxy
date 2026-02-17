# Quick Start

## 1. Set up bot account

On your Matrix homeserver, create a bot account (e.g., `@copilot:eddpod.com`). Note the credentials.

## 2. Configure

```bash
cp .env.example .env
# Edit .env with:
# - MATRIX_HOMESERVER (your homeserver URL)
# - MATRIX_BOT_USER_ID (e.g., @copilot:eddpod.com)
# - MATRIX_ACCESS_TOKEN (from first login, or set MATRIX_BOT_PASSWORD)
# - WEBHOOK_SECRET (any string, use same in agent-shell config)
# - ALLOWED_USERS (comma-separated, e.g., @edd:eddpod.com)
```

## 3. First run (with uv)

```bash
uv sync
uv run matrix_proxy_bot
```

On first run with password, bot prints access token and device ID. Add these to `.env`:

```
MATRIX_ACCESS_TOKEN=syt_xxx...
MATRIX_DEVICE_ID=ABCD123
```

Then run again (no password needed):

```bash
uv run matrix_proxy_bot
```

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

Should post to your Matrix room.

## 5. Enable E2E Encryption (Optional)

For user verification and encrypted messages:

```bash
# Install build dependencies (one-time)
brew install libolm cmake autoconf automake libtool  # macOS
# OR: sudo apt-get install libolm-dev cmake build-essential libssl-dev  # Linux

# Install with E2E support
uv sync --extra e2e

# Set password in .env, then run:
uv run matrix_proxy_bot
```

Bot will bootstrap cross-signing keys and be ready for verification (green shield in Element).

## Commands

```bash
uv sync              # Install/update dependencies
uv run matrix_proxy_bot  # Run the bot
uv run pytest        # Run tests
```

## Next: Connect agent-shell

See agent-shell-matrix-remote.el for Emacs integration.
