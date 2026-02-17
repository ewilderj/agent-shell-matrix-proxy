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
from nio import (
    AsyncClient,
    RoomMessageText,
    SyncResponse,
)
from nio.responses import LoginResponse, LoginError

# E2E encryption (optional)
try:
    from nio import (
        KeyVerificationStart,
        KeyVerificationAccept,
        KeyVerificationKey,
        KeyVerificationMac,
        KeyVerificationCancel,
    )
    from nio.crypto import Sas
    from matrix_proxy_bot.cross_signing import (
        bootstrap_cross_signing,
        load_signing_keys,
        _inject_master_key_mac,
    )
    HAS_E2E = True
except ImportError:
    HAS_E2E = False
    logger = logging.getLogger(__name__)
    logger.warning(
        "E2E encryption not available. "
        "Install with: pip install -e '.[e2e]' and build dependencies."
    )

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
    """Matrix relay bot for agent-shell sessions with E2E encryption."""

    def __init__(self, config: Config, db_path: Path):
        self.config = config
        self.db_path = db_path
        self.db = SessionDB(db_path)

        # Matrix client with encryption
        self.client = AsyncClient(config.homeserver, config.user_id)
        self.sync_task = None
        self.webhook_server = None

        # Encryption
        self.store_dir = Path.home() / ".matrix-proxy-bot"
        self.store_dir.mkdir(exist_ok=True)
        self.cross_signing_keys = None
        self.sas_in_progress: dict[str, Sas] = {}

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
            if isinstance(response, LoginError):
                raise RuntimeError(f"Login failed: {response.message}")
            logger.info(f"Logged in. Access token: {self.client.access_token}")
            logger.info(f"Device ID: {self.client.device_id}")
            logger.info("Save these to .env to avoid re-login")

        # Set up encryption
        logger.info("Setting up E2E encryption...")
        await self._setup_encryption()

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
        """Sync with Matrix homeserver, listen for messages and verification."""
        while True:
            try:
                sync = await self.client.sync(30000)  # 30s timeout

                if isinstance(sync, SyncResponse):
                    # Handle room invites (only from allowed users)
                    for room_id in sync.rooms.invite.keys():
                        # Get inviter from room state
                        room = self.client.rooms.get(room_id)
                        if room and room.inviter and room.inviter not in self.config.allowed_users:
                            logger.warning(
                                f"Ignoring invite from {room.inviter} (not in allowed users)"
                            )
                            continue
                        logger.info(f"Invited to room {room_id}, auto-joining...")
                        await self.client.join(room_id)

                    # Handle messages in joined rooms (only from allowed users)
                    for room_id, room_info in sync.rooms.join.items():
                        for event in room_info.timeline.events:
                            if isinstance(event, RoomMessageText):
                                if event.sender not in self.config.allowed_users:
                                    logger.debug(
                                        f"Ignoring message from {event.sender} (not in allowed users)"
                                    )
                                    continue
                                await self._handle_room_message(room_id, event)
                            elif HAS_E2E:
                                # Handle E2E verification events
                                if isinstance(event, KeyVerificationStart):
                                    await self._handle_key_verification_start(room_id, event)
                                elif isinstance(event, KeyVerificationKey):
                                    await self._handle_key_verification_key(room_id, event)
                                elif isinstance(event, KeyVerificationMac):
                                    await self._handle_key_verification_mac(room_id, event)
                                elif isinstance(event, KeyVerificationCancel):
                                    await self._handle_key_verification_cancel(room_id, event)

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

    async def _setup_encryption(self):
        """Set up E2E encryption and cross-signing (if available)."""
        if not HAS_E2E:
            logger.info("E2E encryption not available (plain-text mode)")
            return
        
        # Load or bootstrap cross-signing keys
        self.cross_signing_keys = load_signing_keys(str(self.store_dir))
        
        if not self.cross_signing_keys:
            if not self.config.password:
                logger.info(
                    "E2E crypto available, but password not set. "
                    "Set MATRIX_BOT_PASSWORD to enable verification."
                )
                return
            
            logger.info("Bootstrapping cross-signing keys...")
            try:
                self.cross_signing_keys = await bootstrap_cross_signing(
                    self.client, str(self.store_dir), self.config.password
                )
                logger.info("✓ Cross-signing keys installed. Green shield enabled!")
            except Exception as e:
                logger.error(f"Failed to bootstrap cross-signing: {e}")
                self.cross_signing_keys = None
        else:
            logger.info("✓ Using existing cross-signing keys")

    async def _handle_key_verification_start(self, room_id: str, event: KeyVerificationStart):
        """Handle incoming SAS verification request."""
        logger.info(f"SAS verification started by {event.sender} in {room_id}")
        
        # Create SAS session
        sas = Sas.from_key_verification_start(event.content, event.sender, self.client.device_id)
        if sas is None:
            logger.warning("Could not create SAS session")
            return
        
        # Store SAS
        sas_key = f"{room_id}:{event.sender}"
        self.sas_in_progress[sas_key] = sas
        
        # Send SAS accept
        accept_content = sas.get_accept()
        await self.client.to_device(
            "m.key.verification.accept",
            {event.sender: {event.device_id: accept_content}},
        )
        logger.info(f"Sent SAS accept to {event.sender}")

    async def _handle_key_verification_key(self, room_id: str, event: KeyVerificationKey):
        """Handle SAS key exchange."""
        sas_key = f"{room_id}:{event.sender}"
        sas = self.sas_in_progress.get(sas_key)
        
        if not sas:
            logger.warning(f"No SAS for {event.sender}")
            return
        
        # Process key
        sas.set_their_pubkey(event.content["key"])
        logger.info(f"SAS key received from {event.sender}")
        
        # Send our key
        key_content = sas.get_key()
        await self.client.to_device(
            "m.key.verification.key",
            {event.sender: {event.device_id: key_content}},
        )
        
        # Show emoji
        emoji_str = " ".join(emoji[0] for emoji in sas.get_emoji())
        logger.info(f"SAS EMOJIS: {emoji_str}")
        logger.info("Auto-confirming SAS match...")
        
        # Auto-confirm
        mac_content = sas.get_mac()
        
        # Inject master key MAC if we have cross-signing keys
        if self.cross_signing_keys:
            _inject_master_key_mac(
                sas, mac_content, self.cross_signing_keys["master"], event.transaction_id
            )
        
        await self.client.to_device(
            "m.key.verification.mac",
            {event.sender: {event.device_id: mac_content}},
        )
        logger.info(f"SAS MAC sent to {event.sender}")

    async def _handle_key_verification_mac(self, room_id: str, event: KeyVerificationMac):
        """Handle SAS MAC verification completion."""
        sas_key = f"{room_id}:{event.sender}"
        sas = self.sas_in_progress.get(sas_key)
        
        if not sas:
            logger.warning(f"No SAS for {event.sender}")
            return
        
        # Verify their MAC
        try:
            sas.verify_mac(event.content["mac"], event.content.get("keys"))
            logger.info(f"✓ SAS verification successful with {event.sender}")
            
            # Mark device as trusted
            self.client.verify_device(sas.other_olm_device)
            logger.info(f"✓ Device {sas.other_device_id} marked as trusted")
            
        except ValueError as e:
            logger.error(f"SAS verification failed: {e}")
        finally:
            del self.sas_in_progress[sas_key]

    async def _handle_key_verification_cancel(self, room_id: str, event: KeyVerificationCancel):
        """Handle SAS cancellation."""
        sas_key = f"{room_id}:{event.sender}"
        if sas_key in self.sas_in_progress:
            logger.info(f"SAS cancelled by {event.sender}: {event.content.get('reason')}")
            del self.sas_in_progress[sas_key]

    async def stop(self):
        """Stop bot."""
        logger.info("Shutting down...")
        if self.webhook_server:
            await self.webhook_server.shutdown()
        if self.sync_task:
            self.sync_task.cancel()
        await self.client.close()
