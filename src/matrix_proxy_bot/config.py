"""Configuration management."""

import os
from dataclasses import dataclass, field


def _get_homeserver():
    return os.getenv("MATRIX_HOMESERVER", "https://eddpod.com").strip()


def _get_user_id():
    return os.getenv("MATRIX_BOT_USER_ID", "@proxy:eddpod.com").strip()


def _get_password():
    return os.getenv("MATRIX_BOT_PASSWORD", "").strip()


def _get_access_token():
    return os.getenv("MATRIX_ACCESS_TOKEN", "").strip()


def _get_device_id():
    return os.getenv("MATRIX_DEVICE_ID", "").strip()


def _get_bot_name():
    return os.getenv("MATRIX_BOT_NAME", "proxy").strip()


def _get_webhook_host():
    return os.getenv("WEBHOOK_HOST", "127.0.0.1").strip()


def _get_webhook_port():
    return int(os.getenv("WEBHOOK_PORT", "8765"))


def _get_webhook_secret():
    return os.getenv("WEBHOOK_SECRET", "secret").strip()


def _get_log_level():
    return os.getenv("LOG_LEVEL", "INFO").strip()


@dataclass
class Config:
    """Bot configuration from environment."""

    # Matrix
    homeserver: str = field(default_factory=_get_homeserver)
    user_id: str = field(default_factory=_get_user_id)
    password: str = field(default_factory=_get_password)
    access_token: str = field(default_factory=_get_access_token)
    device_id: str = field(default_factory=_get_device_id)
    bot_name: str = field(default_factory=_get_bot_name)

    # Webhook
    webhook_host: str = field(default_factory=_get_webhook_host)
    webhook_port: int = field(default_factory=_get_webhook_port)
    webhook_secret: str = field(default_factory=_get_webhook_secret)

    # Logging
    log_level: str = field(default_factory=_get_log_level)

    def validate(self) -> None:
        """Validate required configuration."""
        if not self.homeserver:
            raise ValueError("MATRIX_HOMESERVER required")
        if not self.user_id:
            raise ValueError("MATRIX_BOT_USER_ID required")
        if not (self.password or self.access_token):
            raise ValueError("MATRIX_BOT_PASSWORD or MATRIX_ACCESS_TOKEN required")
