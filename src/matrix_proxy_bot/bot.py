"""Main bot implementation with handoff and relay logic."""

import asyncio
import logging
import json
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

import markdown
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import uvicorn
from nio import AsyncClient, RoomMessageText, SyncResponse, RoomCreateResponse, RoomVisibility
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
        "Install with: uv sync --extra e2e (requires libolm build deps)"
    )

from matrix_proxy_bot.config import Config
from matrix_proxy_bot.db import SessionDB

logger = logging.getLogger(__name__)


# Request/Response models
class HandoffRequest(BaseModel):
    """Initiate handoff from agent-shell to Matrix."""
    session_id: str
    hostname: str
    webhook_url: str
    webhook_secret: str
    message: Optional[str] = None
    quiet_mode: bool = False
    ttl_seconds: Optional[int] = None


class HandoffResponse(BaseModel):
    """Response to handoff request."""
    status: str
    room_id: str
    room_url: str
    session_id: str
    session_hash: str


class WebhookMessageRequest(BaseModel):
    """Response message from agent-shell webhook."""
    room_id: str
    session_id: str
    response_text: Optional[str] = None
    format: Optional[str] = None  # plain, markdown, html
    formatted_body: Optional[str] = None
    action: Optional[str] = None  # for command responses


class SessionStatusResponse(BaseModel):
    """Session status query response."""
    room_id: str
    session_id: str
    session_hash: str
    hostname: str
    owner: str
    initiated_by: Optional[str]
    initiated_at: str
    webhook_url: str
    quiet_mode: bool
    last_message: Optional[str]
    handoff_expires_at: Optional[str]


class CommandParser:
    """Parse room messages for handoff commands."""
    
    COMMANDS = {
        "!return": ("handoff_end", "Hand session back to Emacs"),
        "!close": ("close_session", "Archive and close session"),
        "!status": ("session_status", "Show current session status"),
        "!help": ("help", "Show available commands"),
    }
    
    @classmethod
    def parse(cls, message: str) -> dict:
        """Parse message for commands. Returns {is_command, command, action, args, error?}"""
        message = message.strip()
        
        if not message.startswith("!"):
            return {"is_command": False, "raw": message}
        
        parts = message.split(None, 1)
        command = parts[0]
        args = parts[1].split() if len(parts) > 1 else []
        
        if command in cls.COMMANDS:
            action, desc = cls.COMMANDS[command]
            return {
                "is_command": True,
                "command": command,
                "action": action,
                "args": args,
                "raw": message
            }
        
        return {
            "is_command": True,
            "command": command,
            "action": None,
            "args": args,
            "raw": message,
            "error": f"Unknown command: {command}"
        }


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
        self.ttl_task = None

        # Encryption
        self.store_dir = Path.home() / ".matrix-proxy-bot"
        self.store_dir.mkdir(exist_ok=True)
        self.cross_signing_keys = None
        self.sas_in_progress: dict[str, Sas] = {}

        # FastAPI app for webhook server
        self.app = FastAPI(title="matrix-proxy-bot")
        self._setup_routes()

    def _setup_routes(self):
        """Set up FastAPI routes."""

        @self.app.post("/handoff", response_model=HandoffResponse)
        async def handoff(req: HandoffRequest, authorization: str = Header(None)):
            """Initiate handoff from agent-shell to Matrix."""
            if not self._validate_auth(authorization):
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            logger.info(f"Handoff request: {req.hostname}-{req.session_id}")
            
            try:
                # Check if we already have a room for this session
                existing = await self.db.find_session_by_id(req.session_id, req.hostname)
                
                if existing:
                    # Reuse existing room
                    room_id = existing["room_id"]
                    session_hash = existing["session_hash"]
                    logger.info(f"Reusing existing room {room_id} for session {req.session_id}")
                    
                    # Always reinvite users on room reuse to ensure they can access it
                    for user_id in self.config.allowed_users:
                        try:
                            logger.info(f"Reinviting {user_id} to existing room {room_id}...")
                            await self.client.room_invite(room_id, user_id)
                            logger.info(f"Reinvite successful for {user_id}")
                        except Exception as invite_err:
                            logger.error(f"Reinvite failed for {user_id}: {invite_err}")
                    
                    # Update owner back to matrix (was emacs, now handing off again)
                    await self.db.set_owner(room_id, "matrix")
                else:
                    # Create new room
                    session_hash = hashlib.sha256(req.session_id.encode()).hexdigest()[:8]
                    room_name = f"agent-{req.hostname}-{session_hash}"
                    
                    result = await self.client.room_create(
                        name=room_name,
                        topic=f"Agent shell session from {req.hostname}",
                        invite=self.config.allowed_users,
                        visibility=RoomVisibility.private
                    )
                    
                    if not isinstance(result, RoomCreateResponse):
                        logger.error(f"Room creation failed: {result}")
                        raise HTTPException(status_code=500, detail="Room creation failed")
                    
                    room_id = result.room_id
                    logger.info(f"Created room {room_id}")
                    
                    # Create session in DB
                    await self.db.create_session(
                        room_id=room_id,
                        session_id=req.session_id,
                        session_hash=session_hash,
                        hostname=req.hostname,
                        webhook_url=req.webhook_url,
                        webhook_secret=req.webhook_secret,
                        quiet_mode=req.quiet_mode,
                        ttl_seconds=req.ttl_seconds,
                        initiated_by=self.config.user_id
                    )
                
                # Post initial message
                init_message = f"🔄 Session handed off from {req.hostname}"
                await self.send_to_room(room_id, init_message)
                
                # Post context as formatted markdown if provided
                if req.message:
                    logger.info(f"Context message: {len(req.message)} chars")
                    html = markdown.markdown(
                        req.message,
                        extensions=['fenced_code', 'tables'])
                    await self.send_to_room(
                        room_id, req.message, html,
                        "org.matrix.custom.html")
                
                # Build room URL
                room_url = f"https://element.io/#/room/{room_id}"
                
                return HandoffResponse(
                    status="handoff_started",
                    room_id=room_id,
                    room_url=room_url,
                    session_id=req.session_id,
                    session_hash=session_hash
                )
            
            except Exception as e:
                import traceback
                logger.error(f"Handoff error: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/typing")
        async def set_typing(req: dict, authorization: str = Header(None)):
            """Set typing indicator in a Matrix room."""
            if not self._validate_auth(authorization):
                raise HTTPException(status_code=401, detail="Unauthorized")
            try:
                room_id = req.get("room_id", "")
                is_typing = req.get("typing", True)
                await self.client.room_typing(room_id, is_typing, timeout=30000)
                return {"status": "ok"}
            except Exception as e:
                logger.warning(f"Typing indicator error: {e}")
                return {"status": "error", "detail": str(e)}

        @self.app.post("/webhook/message")
        async def webhook_message(req: WebhookMessageRequest, authorization: str = Header(None)):
            """Relay response from agent-shell back to Matrix."""
            if not self._validate_auth(authorization):
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            logger.info(f"Webhook message for {req.room_id}")
            
            # Verify session exists
            session = await self.db.get_session(req.room_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            try:
                # Command response (handoff_end, close_session, etc.)
                if req.action:
                    await self._handle_command_response(req.room_id, req.action, req.session_id)
                
                # Message response
                elif req.response_text:
                    # Use formatted_body if available, else wrap as [Agent] message
                    if req.formatted_body and req.format:
                        message = req.response_text
                        if not session.get("quiet_mode"):
                            await self.send_to_room(req.room_id, message, req.formatted_body, req.format)
                    else:
                        message = req.response_text
                        html = markdown.markdown(
                            message,
                            extensions=['fenced_code', 'tables'])
                        if not session.get("quiet_mode"):
                            await self.send_to_room(
                                req.room_id, message, html,
                                "org.matrix.custom.html")
                
                await self.db.touch(req.room_id)
                
                return {"status": "message_posted", "room_id": req.room_id}
            
            except Exception as e:
                logger.error(f"Webhook message error: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/session/{room_id}", response_model=SessionStatusResponse)
        async def get_session(room_id: str, authorization: str = Header(None)):
            """Query session status."""
            if not self._validate_auth(authorization):
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            session = await self.db.get_session(room_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            return SessionStatusResponse(
                room_id=session["room_id"],
                session_id=session["session_id"],
                session_hash=session["session_hash"],
                hostname=session["hostname"],
                owner=session["owner"],
                initiated_by=session["initiated_by"],
                initiated_at=session["initiated_at"],
                webhook_url=session["agent_shell_webhook_url"],
                quiet_mode=session["quiet_mode"],
                last_message=session["last_message_at"],
                handoff_expires_at=session["handoff_expires_at"]
            )

        @self.app.get("/sessions")
        async def list_sessions(authorization: str = Header(None)):
            """List all active sessions."""
            if not self._validate_auth(authorization):
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            sessions = await self.db.list_sessions()
            return {
                "sessions": [
                    {
                        "room_id": s["room_id"],
                        "hostname": s["hostname"],
                        "owner": s["owner"],
                        "initiated_at": s["initiated_at"]
                    }
                    for s in sessions
                ],
                "total": len(sessions)
            }

    def _validate_auth(self, auth_header: Optional[str]) -> bool:
        """Validate webhook authorization header."""
        if not auth_header:
            logger.debug("No authorization header provided")
            return False
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            is_valid = token == self.config.webhook_secret
            if not is_valid:
                logger.debug(f"Auth validation failed: token={token[:20]}..., expected={self.config.webhook_secret[:20]}...")
            return is_valid
        logger.debug(f"Invalid auth format: {auth_header[:30]}...")
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

        # Register event callbacks
        # Wrapper to adapt callback signature with exception handling
        async def on_message(room, event):
            try:
                await self._handle_room_message(room.room_id, event)
            except Exception as e:
                logger.exception(f"Error handling room message: {e}")
        
        self.client.add_event_callback(on_message, RoomMessageText)

        # Start webhook server (runs in background)
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
        asyncio.create_task(self.webhook_server.serve())

        # Start TTL scheduler (runs in background)
        logger.info("Starting TTL scheduler...")
        asyncio.create_task(self._ttl_scheduler())

        # Start Matrix sync loop as a background task too
        logger.info("Starting Matrix sync loop...")
        asyncio.create_task(self._sync_loop())

    async def _ttl_scheduler(self):
        """Background task to auto-return expired sessions."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every 60s
                
                expired = await self.db.get_expired_sessions()
                for session in expired:
                    room_id = session["room_id"]
                    webhook_url = session["agent_shell_webhook_url"]
                    webhook_secret = session["agent_shell_secret"]
                    
                    logger.info(f"TTL expired for session {room_id}")
                    
                    # Notify agent-shell
                    try:
                        await self._call_webhook(
                            webhook_url,
                            webhook_secret,
                            {"action": "handoff_end", "reason": "ttl_expired"}
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify webhook: {e}")
                    
                    # Return to Emacs
                    await self.db.set_owner(room_id, "emacs")
                    await self.send_to_room(room_id, "⏰ Session auto-returned to Emacs (TTL expired)")
            
            except Exception as e:
                logger.error(f"TTL scheduler error: {e}")
                await asyncio.sleep(5)

    async def _sync_loop(self):
        """Sync with Matrix homeserver, listen for messages."""
        logger.info("Starting sync_forever loop...")
        try:
            await self.client.sync_forever(timeout=30000)
        except Exception as e:
            logger.exception(f"Sync loop crashed: {e}")
            # sync_forever crashed, wait and restart
            await asyncio.sleep(5)
            logger.info("Restarting sync loop...")
            asyncio.create_task(self._sync_loop())

    async def _handle_room_message(self, room_id: str, event: RoomMessageText):
        """Handle incoming Matrix room message."""
        try:
            sender = event.sender
            body = event.body

            # Skip bot's own messages
            if sender == self.config.user_id:
                return

            logger.info(f"Matrix message in {room_id}: {body[:50]}...")

            # Get session
            session = await self.db.get_session(room_id)
            if not session:
                logger.debug(f"No session for {room_id}, ignoring message")
                return

            # Only relay if owner is 'matrix'
            if session["owner"] != "matrix":
                logger.debug(f"Session owner is {session['owner']}, not relaying")
                return

            # Check if message is a command
            parsed = CommandParser.parse(body)
            
            if parsed.get("is_command"):
                await self._handle_command(room_id, parsed, sender)
            else:
                # Relay to agent-shell webhook
                await self._relay_to_webhook(room_id, body, sender)
        except Exception as e:
            logger.exception(f"Error handling room message in {room_id}: {e}")

    async def _handle_command(self, room_id: str, parsed: dict, sender: str):
        """Execute ! command."""
        try:
            if parsed.get("error"):
                await self.send_to_room(room_id, f"❌ {parsed['error']}")
                return

            action = parsed.get("action")
            
            if action == "handoff_end":
                await self._return_to_emacs(room_id)
            
            elif action == "close_session":
                await self._close_session(room_id)
            
            elif action == "session_status":
                await self._show_status(room_id)
            
            elif action == "help":
                help_text = """Available commands:
!return  — Hand session back to Emacs
!close   — Archive session
!status  — Show session status
!help    — Show this help"""
                await self.send_to_room(room_id, help_text)
        except Exception as e:
            logger.error(f"Command handler error: {e}", exc_info=True)

    async def _return_to_emacs(self, room_id: str):
        """Return session to Emacs."""
        session = await self.db.get_session(room_id)
        if not session:
            return

        # Notify agent-shell
        try:
            await self._call_webhook(
                session["agent_shell_webhook_url"],
                session["agent_shell_secret"],
                {"action": "handoff_end"}
            )
        except Exception as e:
            logger.error(f"Failed to notify webhook: {e}")

        # Update owner
        await self.db.set_owner(room_id, "emacs")
        await self.send_to_room(room_id, "✓ Session returned to Emacs")

    async def _close_session(self, room_id: str):
        """Close and archive session."""
        session = await self.db.get_session(room_id)
        if not session:
            return

        await self.db.set_owner(room_id, "emacs")
        await self.send_to_room(room_id, "🔒 Session closed and archived")

    async def _show_status(self, room_id: str):
        """Show session status."""
        session = await self.db.get_session(room_id)
        if not session:
            return

        status_text = f"""**Session Status**
Hostname: {session['hostname']}
Session: {session['session_hash']}
Owner: {session['owner']}
Started: {session['initiated_at']}
Last message: {session['last_message_at']}"""
        
        if session["handoff_expires_at"]:
            expires = datetime.fromisoformat(session["handoff_expires_at"])
            time_left = (expires - datetime.utcnow()).total_seconds() / 60
            status_text += f"\nExpires: {time_left:.0f}m"

        await self.send_to_room(room_id, status_text)

    async def _relay_to_webhook(self, room_id: str, message: str, sender: str):
        """Relay user message to agent-shell webhook."""
        session = await self.db.get_session(room_id)
        if not session:
            return

        payload = {
            "room_id": room_id,
            "session_id": session["session_id"],
            "sender": sender,
            "message": message
        }

        try:
            response = await self._call_webhook(
                session["agent_shell_webhook_url"],
                session["agent_shell_secret"],
                payload
            )
            logger.info(f"Relayed to webhook: {message[:50]}...")
        except asyncio.TimeoutError:
            await self.send_to_room(room_id, "❌ Webhook timeout (agent-shell not responding)")
        except Exception as e:
            logger.error(f"Webhook relay error: {e}")
            await self.send_to_room(room_id, f"❌ Relay error: {str(e)[:100]}")

    async def _handle_command_response(self, room_id: str, action: str, session_id: str):
        """Handle command response from webhook."""
        if action == "handoff_end":
            await self.db.set_owner(room_id, "emacs")
            await self.send_to_room(room_id, "✓ Session returned to Emacs")

    async def _call_webhook(self, url: str, secret: str, payload: dict) -> dict:
        """Call webhook endpoint (timeout after 5s)."""
        import aiohttp
        
        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json"
        }
        
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                return await resp.json()

    async def send_to_room(self, room_id: str, message: str, formatted_body: str = None, format_type: str = None):
        """Send message to Matrix room with optional HTML formatting.
        
        If formatted_body and format_type are provided, sends as formatted message.
        Otherwise sends as plain text.
        """
        try:
            content = {
                "msgtype": "m.text",
                "body": message
            }
            
            # Add formatting if provided
            if formatted_body and format_type:
                content["formatted_body"] = formatted_body
                content["format"] = format_type
            
            await self.client.room_send(room_id, "m.room.message", content)
            logger.debug(f"Sent to {room_id}: {message[:50]}...")
        except Exception as e:
            logger.error(f"Failed to send to {room_id}: {e}")

    async def _setup_encryption(self):
        """Set up E2E encryption (if available)."""
        if not HAS_E2E:
            logger.info("E2E encryption disabled (not installed)")
            return

        try:
            keys_path = self.store_dir / f"{self.client.device_id}_keys.json"
            
            if keys_path.exists():
                self.cross_signing_keys = load_signing_keys(keys_path)
                logger.info("Loaded existing cross-signing keys")
            else:
                logger.info("Bootstrapping cross-signing keys...")
                await bootstrap_cross_signing(self.client, self.store_dir)
                self.cross_signing_keys = load_signing_keys(keys_path)

            # Enable E2E
            await self.client.keys_upload()
            logger.info("E2E encryption ready")
        except Exception as e:
            logger.error(f"E2E setup error: {e}")

    async def _handle_key_verification_start(self, room_id: str, event):
        """Handle key verification start (SAS)."""
        if not HAS_E2E:
            return

        logger.info(f"Key verification start from {event.sender}")
        sas = Sas.from_key_verification_start(event, self.client.user_id, self.client.device_id)
        self.sas_in_progress[room_id] = sas

        await self.client.accept_key_verification(event.transaction_id)

    async def _handle_key_verification_key(self, room_id: str, event):
        """Handle key verification key exchange."""
        if not HAS_E2E or room_id not in self.sas_in_progress:
            return

        sas = self.sas_in_progress[room_id]
        sas.set_their_pubkey(event.key)

        emojis = sas.get_emoji()
        logger.info(f"SAS emojis: {emojis}")

        key_mac_list = sas.get_mac()
        await self.client.send_key_verification_mac(event.transaction_id, key_mac_list)

    async def _handle_key_verification_mac(self, room_id: str, event):
        """Handle key verification MAC."""
        if not HAS_E2E or room_id not in self.sas_in_progress:
            return

        sas = self.sas_in_progress[room_id]
        
        if sas.verify_mac(event.mac, event.transaction_id):
            logger.info("Verification successful!")
            
            if self.cross_signing_keys:
                _inject_master_key_mac(
                    self.cross_signing_keys,
                    sas.we_started_it,
                    sas.sas_nonemojis
                )
            
            await self.client.confirm_key_verification(event.transaction_id)
            del self.sas_in_progress[room_id]
        else:
            logger.warning("Verification failed!")
            await self.client.cancel_key_verification(event.transaction_id, "m.key_mismatch")

    async def _handle_key_verification_cancel(self, room_id: str, event):
        """Handle key verification cancel."""
        if room_id in self.sas_in_progress:
            del self.sas_in_progress[room_id]
        logger.info(f"Verification cancelled: {event.reason}")
