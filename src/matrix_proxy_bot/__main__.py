"""Matrix Proxy Bot - webhooks relay for agent-shell sessions."""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Force unbuffered output
sys.stdout = open(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = open(sys.stderr.fileno(), 'w', buffering=1)

from dotenv import load_dotenv

from matrix_proxy_bot.bot import ProxyBot
from matrix_proxy_bot.config import Config

# Load .env file
env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if not env_path.exists():
    # Fallback: try current working directory
    env_path = Path.cwd() / ".env"

print(f"[STARTUP] Loading .env from: {env_path}", file=sys.stderr)
print(f"[STARTUP] .env exists: {env_path.exists()}", file=sys.stderr)
result = load_dotenv(env_path)
print(f"[STARTUP] load_dotenv returned: {result}", file=sys.stderr)
if env_path.exists():
    print(f"[STARTUP] WEBHOOK_SECRET loaded: {len(os.getenv('WEBHOOK_SECRET', ''))} chars", file=sys.stderr)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


async def main():
    """Run the proxy bot."""
    config = Config()

    # Ensure database exists
    db_dir = Path.home() / ".matrix-proxy-bot"
    db_dir.mkdir(exist_ok=True)
    db_path = db_dir / "sessions.db"

    bot = ProxyBot(config=config, db_path=db_path)

    try:
        await bot.start()
        # After start() returns, background tasks are running
        logger.info("Bot started, keeping event loop alive...")
        # Keep the event loop alive indefinitely
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await bot.stop()
    except Exception as e:
        import traceback
        logger.error(f"Bot error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
