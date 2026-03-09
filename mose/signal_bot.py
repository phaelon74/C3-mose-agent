"""Signal bot: relay messages to the agent loop via signal-cli JSON-RPC."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextvars import ContextVar
from typing import Any

from mose.agent import Agent
from mose.config import SignalConfig
from mose.observe import get_logger, log_event

logger = get_logger("signal")

MAX_MESSAGE_LENGTH = 4000  # Practical limit for Signal readability

_approval_ctx: ContextVar[dict] = ContextVar("signal_approval_ctx", default={})


def set_approval_context(sender: str, bot: "MoseSignalBot") -> None:
    """Set context for sre_execute approval (sender phone, bot)."""
    _approval_ctx.set({"sender": sender, "bot": bot})


async def _signal_approval_callback(command: str, reason: str, target_system: str) -> bool:
    """Prompt user for approval via Signal message. Waits for reply (y/yes/approve) within 60s."""
    ctx = _approval_ctx.get()
    if not ctx:
        return False
    sender = ctx.get("sender")
    bot = ctx.get("bot")
    if not sender or not bot:
        return False

    prompt = (
        f"SRE Execute Approval\n\n"
        f"System: {target_system}\n"
        f"Reason: {reason}\n"
        f"Command: {command[:500]}{'...' if len(command) > 500 else ''}\n\n"
        f"Reply with 'y', 'yes', or 'approve' within 60 seconds."
    )
    await bot._send_message(sender, prompt)

    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    bot._pending_approval[sender] = future

    try:
        approved = await asyncio.wait_for(future, 60)
    except asyncio.TimeoutError:
        await bot._send_message(sender, "Approval timed out. Execution denied.")
        approved = False
    finally:
        bot._pending_approval.pop(sender, None)

    if not approved:
        await bot._send_message(sender, "Execution denied.")
    return approved


def _session_id_for(source_number: str) -> str:
    """Derive a session ID from the sender phone number."""
    normalized = source_number.strip().replace(" ", "")
    h = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    return f"signal-{h}"


def _split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a long message into chunks that fit the limit."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = text.rfind(" ", 0, max_len)
        if split_at == -1:
            split_at = max_len

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


def _format_status(tool_name: str, arguments: str) -> str:
    """Format a tool call into a short status message."""
    try:
        args = json.loads(arguments) if arguments else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    if tool_name == "web_search":
        return f"Searching: {args.get('query', arguments)}"
    if tool_name == "web_fetch":
        return f"Reading {args.get('url', arguments)}"
    if tool_name == "bash":
        cmd = args.get("command", arguments)
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"Running: {cmd}"
    if tool_name == "sre_execute":
        cmd = args.get("command", arguments)
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"Execute (approval required): {cmd}"
    if tool_name == "read_file":
        return f"Reading {args.get('path', arguments)}"
    if tool_name == "write_file":
        return f"Writing {args.get('path', arguments)}"
    if tool_name in ("delegate", "code_task"):
        return f"Working: {args.get('task', arguments)}"
    if tool_name == "use_tool":
        return f"Using {args.get('name', arguments)}"
    return f"{tool_name}..."


def _extract_message_from_envelope(envelope: dict) -> tuple[str | None, str | None, str | None]:
    """Extract (source_number, message_text, group_id) from a receive envelope."""
    source = envelope.get("source") or envelope.get("sourceNumber") or ""
    if not source:
        return None, None, None

    # dataMessage: direct incoming message
    data_msg = envelope.get("dataMessage")
    if data_msg and isinstance(data_msg, dict):
        msg = data_msg.get("message") or ""
        group_info = data_msg.get("groupInfo") or {}
        group_id = group_info.get("groupId") if isinstance(group_info, dict) else None
        return source, (msg if isinstance(msg, str) else ""), group_id

    # syncMessage: our own sent messages synced from primary - ignore
    if envelope.get("syncMessage"):
        return None, None, None

    return source, None, None


class MoseSignalBot:
    """Signal client that connects to signal-cli daemon and relays messages to the Mose agent."""

    def __init__(self, agent: Agent, config: SignalConfig) -> None:
        self.agent = agent
        self.config = config
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._rpc_id = 0
        self._rpc_pending: dict[str, asyncio.Future[dict]] = {}
        self._pending_approval: dict[str, asyncio.Future[bool]] = {}
        self._running = False

    async def _connect(self) -> None:
        """Connect to the signal-cli TCP daemon."""
        self._reader, self._writer = await asyncio.open_connection(
            self.config.daemon_host,
            self.config.daemon_port,
        )
        log_event(logger, "signal_connected", host=self.config.daemon_host, port=self.config.daemon_port)

    def _next_id(self) -> str:
        self._rpc_id += 1
        return f"mose-{self._rpc_id}"

    async def _send_rpc(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        req_id = self._next_id()
        req = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params:
            req["params"] = params

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._rpc_pending[req_id] = future

        line = json.dumps(req) + "\n"
        if self._writer:
            self._writer.write(line.encode())
            await self._writer.drain()

        try:
            return await asyncio.wait_for(future, 30)
        finally:
            self._rpc_pending.pop(req_id, None)

    async def _send_message(self, recipient: str, text: str) -> None:
        """Send a message to a recipient. Splits long messages into chunks."""
        chunks = _split_message(text)
        for chunk in chunks:
            await self._send_rpc("send", {"recipient": [recipient], "message": chunk})

    def _handle_rpc_line(self, line: str) -> None:
        """Process one JSON-RPC line (response or notification)."""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("signal_invalid_json", line=line[:200])
            return

        req_id = obj.get("id")

        if req_id is not None:
            # Response to our request (id may be echoed as string or number)
            future = self._rpc_pending.get(str(req_id))
            if future and not future.done():
                if "error" in obj:
                    future.set_exception(RuntimeError(obj["error"].get("message", "RPC error")))
                else:
                    future.set_result(obj.get("result", {}))
            return

        # Notification (no id)
        method = obj.get("method")
        params = obj.get("params") or {}

        if method == "receive":
            # Envelope can be at params.envelope (auto mode) or params.result.envelope (subscribe mode)
            envelope = params.get("envelope")
            if not envelope and "result" in params:
                envelope = params["result"].get("envelope")
            if envelope:
                asyncio.create_task(self._handle_message(envelope))

    async def _handle_message(self, envelope: dict) -> None:
        """Process an incoming message envelope."""
        source, message_text, group_id = _extract_message_from_envelope(envelope)
        if not source or message_text is None:
            return

        # Ignore empty messages
        content = message_text.strip() if isinstance(message_text, str) else ""
        if not content:
            return

        # Check if this is an approval reply
        if source in self._pending_approval:
            future = self._pending_approval.get(source)
            if future and not future.done():
                approved = content.lower() in ("y", "yes", "approve")
                future.set_result(approved)
            return

        session_id = _session_id_for(source)
        log_event(logger, "signal_message", session_id=session_id, source=source)

        set_approval_context(source, self)

        async def _send_status(tool_name: str, arguments: str) -> None:
            status = _format_status(tool_name, arguments)
            try:
                await self._send_message(source, status)
            except Exception:
                pass

        try:
            response = await self.agent.process(content, session_id, status_callback=_send_status)
        except Exception:
            logger.exception("Agent processing failed")
            response = "Sorry, I encountered an error processing your message."

        chunks = _split_message(response)
        for chunk in chunks:
            await self._send_message(source, chunk)

    async def _reader_loop(self) -> None:
        """Read JSON-RPC lines from the stream and dispatch them."""
        if not self._reader:
            return
        buf = b""
        try:
            while self._running and self._reader:
                data = await self._reader.read(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line_str = line.decode(errors="replace").strip()
                    if line_str:
                        self._handle_rpc_line(line_str)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("signal_reader_error")
        finally:
            log_event(logger, "signal_reader_stopped")

    async def start(self) -> None:
        """Connect to signal-cli and run the message loop with reconnection."""
        self._running = True
        backoff = 1.0
        max_backoff = 60.0

        while self._running:
            try:
                await self._connect()
                backoff = 1.0
                self._reader_task = asyncio.create_task(self._reader_loop())
                await self._reader_task
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_event(logger, "signal_connection_lost", error=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def close(self) -> None:
        """Stop the bot and close the connection."""
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        log_event(logger, "signal_closed")
