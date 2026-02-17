"""Main bot implementation."""

import asyncio
import logging
import json
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn
from nio import AsyncClient, RoomMessageText, SyncResponse

from matrix_proxy_bot.config import Config
from matrix_proxy_bot.db import SessionDB

logger = logging.getLogger(__name__)


class WebhookMessage(BaseModel):
    """Message from agent-shell webhook."""

    room_id: str
    message: str
    session_id: Optional[str] = None


class HandoffRequest(BaseModel):
    """Handoff request between Matrix and Emacs."""

    room_id: str
    owner: str  # "matrix" or "emacs"


class ProxyBot:
    """Matrix relay bot for agent-shell sessions."""

    def __init__(self, config: Config, db_path: Path):
        self.config = config
        self.db_path = db_path
        self.db = SessionDB(db_path)

        # Matrix client
        self.client = AsyncClient(config.homeserver, config.user_id)
        self.sync_task = None
        self.webhook_server = None

        # WebSocket connections from agent-shell clients
        self.active_connections: dict[str, WebSocket] = {}

        # FastAPI app for webhook server
        self.app = FastAPI(title="matrix-proxy-bot")
        self._setup_routes()

    def _setup_routes(self):
        """Set up FastAPI routes."""

        @self.app.post("/webhook/message")
        async def webhook_message(
            req: WebhookMessage, auth: str = Header(None)
        ):
            """Receive message from agent-shell, forward to Matrix."""
            if not self._validate_auth(auth):
                raise HTTPException(status_code=401, detail="Unauthorized")

            logger.info(
                f"Webhook message from {req.room_id}: {req.message[:50]}..."
            )

            # Forward to Matrix room
            await self.send_to_room(req.room_id, req.message)

            # Store session if new
            if req.session_id:
                existing = await self.db.get_session(req.room_id)
                if not existing:
                    await self.db.create_session(req.room_id, req.session_id)

            await self.db.touch(req.room_id)

            return {"status": "ok"}

        @self.app.post("/handoff")
        async def handoff(req: HandoffRequest, auth: str = Header(None)):
            """Handoff session between Matrix and Emacs."""
            if not self._validate_auth(auth):
                raise HTTPException(status_code=401, detail="Unauthorized")

            if req.owner not in ("matrix", "emacs"):
                raise HTTPException(
                    status_code=400, detail="owner must be 'matrix' or 'emacs'"
                )

            logger.info(f"Handoff {req.room_id} to {req.owner}")
            await self.db.set_owner(req.room_id, req.owner)

            # Notify Matrix room
            message = f"🔄 Handed off to {req.owner}"
            await self.send_to_room(req.room_id, message)

            return {"status": "ok", "owner": req.owner}

        @self.app.get("/session/{room_id}")
        async def get_session(room_id: str, auth: str = Header(None)):
            """Get session status."""
            if not self._validate_auth(auth):
                raise HTTPException(status_code=401, detail="Unauthorized")

            session = await self.db.get_session(room_id)
            return session or {"status": "not found"}

    def _validate_auth(self, auth_header: Optional[str]) -> bool:
        """Validate webhook authorization header."""
        if not auth_header:
            return False
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            return token == self.config.webhook_secret
        return False

    async def start(self):
        """Start bot and webhook server."""
        self.config.validate()

        # Initialize database
        await self.db.initialize()

        # Matrix login
        if self.config.access_token:
            self.client.access_token = self.config.access_token
            self.client.device_id = self.config.device_id
            logger.info("Using cached access token")
        else:
            logger.info("Logging in with password...")
            response = await self.client.login(
                self.config.password, device_name="matrix-proxy-bot"
            )
            if response.status_code != "M_OK":
                raise RuntimeError(f"Login failed: {response}")
            logger.info(f"Logged in. Access token: {self.client.access_token}")
            logger.info(f"Device ID: {self.client.device_id}")
            logger.info("Save these to .env to avoid re-login")

        # Start webhook server
        logger.info(
            f"Starting webhook server on {self.config.webhook_host}:{self.config.webhook_port}"
        )
        config = uvicorn.Config(
            self.app,
            host=self.config.webhook_host,
            port=self.config.webhook_port,
            log_level=self.config.log_level.lower(),
        )
        self.webhook_server = uvicorn.Server(config)
        server_task = asyncio.create_task(self.webhook_server.serve())

        # Start Matrix sync loop
        logger.info("Starting Matrix sync loop...")
        self.sync_task = asyncio.create_task(self._sync_loop())

        # Wait for both
        await asyncio.gather(server_task, self.sync_task)

    async def _sync_loop(self):
        """Sync with Matrix homeserver, listen for messages."""
        async with self.client:
            while True:
                try:
                    sync = await self.client.sync(30000)  # 30s timeout

                    if isinstance(sync, SyncResponse):
                        for room_id, room_info in sync.rooms.join.items():
                            for event in room_info.timeline.events:
                                if isinstance(event, RoomMessageText):
                                    await self._handle_room_message(room_id, event)

                except Exception as e:
                    logger.error(f"Sync error: {e}")
                    await asyncio.sleep(5)

    async def _handle_room_message(self, room_id: str, event: RoomMessageText):
        """Handle incoming Matrix room message."""
        sender = event.sender
        body = event.body

        # Skip bot's own messages
        if sender == self.config.user_id:
            return

        logger.info(f"Matrix message from {sender} in {room_id}: {body[:50]}...")

        # Check session owner
        owner = await self.db.get_owner(room_id)

        if owner == "emacs":
            # Forward to agent-shell via webhook connection
            await self._send_to_emacs(room_id, body, sender)
        else:
            # Owner is matrix, or default behavior
            logger.debug(f"Message for room {room_id} - owner is {owner}")

    async def _send_to_emacs(self, room_id: str, message: str, sender: str):
        """Send Matrix message to connected agent-shell client."""
        payload = json.dumps(
            {"room_id": room_id, "message": message, "sender": sender}
        )

        if room_id in self.active_connections:
            try:
                await self.active_connections[room_id].send_text(payload)
                logger.info(f"Sent to Emacs: {payload[:50]}...")
            except Exception as e:
                logger.error(f"Failed to send to Emacs client: {e}")
                del self.active_connections[room_id]
        else:
            logger.debug(f"No active Emacs client for {room_id}")

    async def send_to_room(self, room_id: str, message: str):
        """Send message to Matrix room."""
        try:
            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": message,
                },
            )
            logger.info(f"Sent to Matrix {room_id}: {message[:50]}...")
        except Exception as e:
            logger.error(f"Failed to send to Matrix: {e}")

    async def stop(self):
        """Stop bot."""
        logger.info("Shutting down...")
        if self.webhook_server:
            await self.webhook_server.shutdown()
        if self.sync_task:
            self.sync_task.cancel()
        await self.client.close()
