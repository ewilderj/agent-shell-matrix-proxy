"""Configuration management."""

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Bot configuration from environment."""

    # Matrix
    homeserver: str = os.getenv("MATRIX_HOMESERVER", "https://eddpod.com").strip()
    user_id: str = os.getenv("MATRIX_BOT_USER_ID", "@proxy:eddpod.com").strip()
    password: str = os.getenv("MATRIX_BOT_PASSWORD", "").strip()
    access_token: str = os.getenv("MATRIX_ACCESS_TOKEN", "").strip()
    device_id: str = os.getenv("MATRIX_DEVICE_ID", "").strip()
    bot_name: str = os.getenv("MATRIX_BOT_NAME", "proxy").strip()

    # Webhook
    webhook_host: str = os.getenv("WEBHOOK_HOST", "127.0.0.1").strip()
    webhook_port: int = int(os.getenv("WEBHOOK_PORT", "8765"))
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "secret").strip()

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO").strip()

    def validate(self) -> None:
        """Validate required configuration."""
        if not self.homeserver:
            raise ValueError("MATRIX_HOMESERVER required")
        if not self.user_id:
            raise ValueError("MATRIX_BOT_USER_ID required")
        if not (self.password or self.access_token):
            raise ValueError("MATRIX_BOT_PASSWORD or MATRIX_ACCESS_TOKEN required")
