"""Matrix Proxy Bot - webhooks relay for agent-shell sessions."""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from matrix_proxy_bot.bot import ProxyBot
from matrix_proxy_bot.config import Config

logging.basicConfig(level=logging.INFO)
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
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
