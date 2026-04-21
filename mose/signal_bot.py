"""Signal bot: relay messages to the agent loop via signal-cli JSON-RPC."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextvars import ContextVar
from typing import Any

from mose.agent import Agent
from mose.config import SignalConfig
from mose.mcp_write_policy import use_tool_needs_approval
from mose.observe import get_logger, log_event

logger = get_logger("signal")

MAX_MESSAGE_LENGTH = 4000  # Practical limit for Signal readability

_approval_ctx: ContextVar[dict] = ContextVar("signal_approval_ctx", default={})

# Set by MoseSignalBot.start(); used by the proactive proposal/review callbacks
# which are invoked outside any specific incoming-message context.
_active_bot: "MoseSignalBot | None" = None


def _format_ts(epoch: float) -> str:
    """Render an epoch timestamp as ISO8601 UTC."""
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        return str(epoch)


async def _signal_skill_propose_callback(
    path: str, slug: str, title: str, description: str, rationale: str, expires_at: float
) -> None:
    """Fire-and-forget Signal notification for a new skill proposal.

    The reply arrives asynchronously via ``_handle_skill_approval_reply``
    below. This function MUST NOT block waiting for a reply — the pending
    state is already durable in SQLite and the decision may arrive in a
    different process (after a restart).
    """
    bot = _active_bot
    if bot is None:
        logger.warning("signal_skill_propose_no_bot", extra={"slug": slug})
        return
    admin_gid = (bot.config.admin_group_id or "").strip()
    if not admin_gid:
        logger.warning("signal_skill_propose_no_admin_group", extra={"slug": slug})
        return

    prompt = (
        "New skill proposal\n\n"
        f"Slug: {slug}\n"
        f"Title: {title}\n"
        f"Description: {description}\n\n"
        f"Rationale:\n{rationale}\n\n"
        f"Proposal file: {path}\n"
        f"Expires: {_format_ts(expires_at)} (UTC)\n\n"
        f"Reply with either of the following to approve:\n"
        f"  approve {slug}\n  yes {slug}\n  y {slug}\n\n"
        f"To reject, use the same syntax with 'reject' / 'no' / 'n'. A bare\n"
        f"'yes' / 'no' also works when exactly one proposal is pending."
    )
    try:
        await bot._send_message(admin_gid, prompt)
    except Exception:
        logger.exception("signal_skill_propose_send_failed", extra={"slug": slug})


async def _signal_skill_reminder_callback(
    slug: str, title: str, description: str, expires_at: float
) -> None:
    """Re-ping the admin after a restart for proposals that are still pending."""
    bot = _active_bot
    if bot is None:
        return
    admin_gid = (bot.config.admin_group_id or "").strip()
    if not admin_gid:
        return
    msg = (
        "Reminder: skill proposal still pending\n\n"
        f"Slug: {slug}\n"
        f"Title: {title}\n"
        f"Description: {description}\n"
        f"Expires: {_format_ts(expires_at)} (UTC)\n\n"
        f"Reply 'approve {slug}' to build, 'reject {slug}' to discard."
    )
    try:
        await bot._send_message(admin_gid, msg)
    except Exception:
        logger.exception("signal_skill_reminder_send_failed", extra={"slug": slug})


async def _signal_skill_recovery_notice(
    still_pending: list[Any],
    expired_while_down: list[Any],
    approved_unbuilt: list[Any],
) -> None:
    """Consolidated startup recovery message.

    Sends exactly ONE Signal message to the admin listing every approval
    that was outstanding when the agent restarted:

    - ``still_pending`` — decision needed (reply ``approve``/``reject``)
    - ``expired_while_down`` — informational only (already rejected)
    - ``approved_unbuilt`` — build will auto-proceed after the grace
      window unless the admin replies ``stop <slug>`` / ``cancel <slug>``

    Sends nothing when all three lists are empty.
    """
    if not still_pending and not expired_while_down and not approved_unbuilt:
        return
    bot = _active_bot
    if bot is None:
        return
    admin_gid = (bot.config.admin_group_id or "").strip()
    if not admin_gid:
        return

    def _title_of(row: Any) -> str:
        payload = getattr(row, "payload", None) or {}
        return (payload.get("title") or row.slug) if isinstance(payload, dict) else row.slug

    lines: list[str] = ["Agent restart recovery"]

    if still_pending:
        lines.append("")
        lines.append(f"Still pending ({len(still_pending)}) — your decision needed:")
        for row in still_pending:
            lines.append(
                f"  - {row.slug} — {_title_of(row)} "
                f"(expires {_format_ts(row.expires_at)} UTC)"
            )
        lines.append("")
        lines.append(
            "Reply 'approve <slug>' to build or 'reject <slug>' to discard. "
            "A bare 'yes' / 'no' works when exactly one is pending."
        )

    if approved_unbuilt:
        import time as _time
        learner = getattr(bot.agent, "_skill_learner", None)
        grace = max(0, int(getattr(learner, "_build_grace_seconds", 900)))
        build_at = _time.time() + grace
        mins = grace // 60 if grace >= 60 else 0
        window_label = f"{mins} min" if mins else f"{grace}s"
        lines.append("")
        lines.append(
            f"Approved but not yet built ({len(approved_unbuilt)}) — "
            f"I'll start building in {window_label} (~{_format_ts(build_at)} UTC). "
            "Reply 'stop <slug>' or 'cancel <slug>' to abort:"
        )
        for row in approved_unbuilt:
            lines.append(f"  - {row.slug} — {_title_of(row)}")

    if expired_while_down:
        lines.append("")
        lines.append(
            f"Expired while I was down ({len(expired_while_down)}) — "
            "no action needed, already moved to skills/rejected/:"
        )
        for row in expired_while_down:
            lines.append(
                f"  - {row.slug} — {_title_of(row)} "
                f"(expired {_format_ts(row.expires_at)} UTC)"
            )

    try:
        await bot._send_message(admin_gid, "\n".join(lines))
    except Exception:
        logger.exception(
            "signal_skill_recovery_send_failed",
            extra={
                "still_pending": len(still_pending),
                "expired_while_down": len(expired_while_down),
                "approved_unbuilt": len(approved_unbuilt),
            },
        )


async def _signal_skill_review_notify(report_path: str, summary: str) -> None:
    """Send a short skill-review summary to the admin via Signal."""
    bot = _active_bot
    if bot is None:
        return
    admin_gid = (bot.config.admin_group_id or "").strip()
    if not admin_gid:
        return
    await bot._send_message(
        admin_gid,
        "Skill Review Report\n\n"
        f"{summary}\n\n"
        f"Full report on disk: {report_path}\n"
        "I made NO changes. Reply with instructions to apply any action.",
    )


_APPROVE_VERBS = {"approve", "yes", "y"}
_REJECT_VERBS = {"reject", "no", "n", "deny"}
_CANCEL_VERBS = {"stop", "cancel", "abort", "halt"}


def _parse_approval_reply(
    text: str,
) -> tuple[str | None, str | None]:
    """Parse an admin reply. Returns ``(slug_or_None, action_or_None)``.

    ``action`` is one of:

    - ``"approve"`` — proceed with building ``slug`` (when pending)
    - ``"reject"`` — discard the pending proposal for ``slug``
    - ``"cancel"`` — abort an approved-but-unbuilt build during its grace
      window (``stop``/``cancel``/``abort``/``halt``)
    - ``None`` — the message is not a decision reply

    When the slug is omitted the caller is expected to resolve it against
    the single outstanding item of the relevant kind.
    """
    tokens = text.strip().split()
    if not tokens:
        return None, None
    verb = tokens[0].lower().rstrip(":,")
    if verb in _APPROVE_VERBS:
        action = "approve"
    elif verb in _REJECT_VERBS:
        action = "reject"
    elif verb in _CANCEL_VERBS:
        action = "cancel"
    else:
        return None, None
    if len(tokens) >= 2:
        candidate = tokens[1].lower().strip(":,;.")
        if candidate.startswith("slug="):
            candidate = candidate[len("slug="):]
        return (candidate or None), action
    return None, action


async def _handle_skill_approval_reply(bot: "MoseSignalBot", group_id: str, text: str) -> bool:
    """If ``text`` looks like a skill-approval reply in the admin group, apply it.

    Understands three action verbs:

    - ``approve`` / ``yes`` / ``y`` — build a pending proposal
    - ``reject`` / ``no`` / ``n`` / ``deny`` — discard a pending proposal
    - ``stop`` / ``cancel`` / ``abort`` / ``halt`` — abort an
      approved-but-unbuilt skill during its startup grace window

    Returns True if the message was consumed (and should NOT be routed to
    the agent), False otherwise.
    """
    admin_gid = (bot.config.admin_group_id or "").strip()
    if not group_id or group_id != admin_gid:
        return False
    slug, action = _parse_approval_reply(text)
    if action is None:
        return False

    memory = getattr(bot.agent, "memory", None)

    if action == "cancel":
        # Resolve bare 'stop'/'cancel' against approved-but-unbuilt orphans.
        if slug is None:
            if memory is None:
                return False
            approved = memory.list_approved_approvals(kind="skill_proposal")
            if len(approved) != 1:
                await bot._send_message(
                    admin_gid,
                    f"{len(approved)} skills in their grace window; please include "
                    f"the slug (e.g. 'stop my-skill').",
                )
                return True
            slug = approved[0].slug
        ok = await bot.agent.cancel_approved_build(slug)
        if ok:
            await bot._send_message(admin_gid, f"Skill build for '{slug}' cancelled.")
        else:
            await bot._send_message(
                admin_gid,
                f"No approved-but-unbuilt skill found for '{slug}' "
                "(already built, already cancelled, or unknown slug).",
            )
        return True

    # Resolve a bare approve/reject verb against the admin's pending queue.
    if slug is None:
        if memory is None:
            return False
        pending = memory.list_pending_approvals(
            kind="skill_proposal", recipient=admin_gid,
        )
        if len(pending) != 1:
            await bot._send_message(
                admin_gid,
                f"{len(pending)} skill proposals pending; please include the slug "
                f"(e.g. 'approve my-skill').",
            )
            return True
        slug = pending[0].slug

    approved = action == "approve"
    from mose.learning import handle_skill_decision
    applied = await handle_skill_decision(slug, approved=approved)
    if applied:
        verb = "approved — building now" if approved else "rejected"
        await bot._send_message(admin_gid, f"Skill '{slug}' {verb}.")
    else:
        await bot._send_message(
            admin_gid, f"No pending proposal found for '{slug}' (already decided or expired)."
        )
    return True


def set_approval_context(incoming_group_id: str, bot: "MoseSignalBot") -> None:
    """Set context for sre_execute approval (incoming Signal group id, bot)."""
    _approval_ctx.set({"incoming_group_id": incoming_group_id, "bot": bot})


async def _signal_approval_callback(command: str, reason: str, target_system: str) -> bool:
    """Prompt admin group for approval. Waits for reply (y/yes/approve) within 60s."""
    ctx = _approval_ctx.get()
    if not ctx:
        return False
    incoming = ctx.get("incoming_group_id")
    bot = ctx.get("bot")
    if not incoming or not bot:
        return False

    admin_gid = (bot.config.admin_group_id or "").strip()
    eng_gid = (bot.config.engagement_group_id or "").strip()
    if not admin_gid:
        return False

    title = "MCP Tool Approval" if target_system.startswith("mcp:") else "SRE Execute Approval"
    prompt = (
        f"{title}\n\n"
        f"System: {target_system}\n"
        f"Reason: {reason}\n"
        f"Command: {command[:500]}{'...' if len(command) > 500 else ''}\n\n"
        f"Reply with 'y', 'yes', or 'approve' within 60 seconds."
    )
    await bot._send_message(admin_gid, prompt)

    if eng_gid and incoming == eng_gid and eng_gid != admin_gid:
        try:
            await bot._send_message(
                eng_gid,
                "Awaiting admin approval in the admin channel…",
            )
        except Exception:
            logger.exception("signal_sre_execute_engagement_notice_failed")

    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    bot._pending_approval[admin_gid] = future

    try:
        approved = await asyncio.wait_for(future, 60)
    except asyncio.TimeoutError:
        await bot._send_message(admin_gid, "Approval timed out. Execution denied.")
        approved = False
    finally:
        bot._pending_approval.pop(admin_gid, None)

    if not approved:
        await bot._send_message(admin_gid, "Execution denied.")
    return approved


def _normalize_group_id(group_id: str | None) -> str | None:
    if group_id is None or not isinstance(group_id, str):
        return None
    s = group_id.strip()
    return s if s else None


def _session_id_for_signal_group(group_id: str, *, admin: bool) -> str:
    """Stable session id from Signal group id (engagement vs admin memory split)."""
    gid = _normalize_group_id(group_id) or ""
    h = hashlib.sha256(gid.encode()).hexdigest()[:16]
    prefix = "signal-grp-adm-" if admin else "signal-grp-eng-"
    return prefix + h


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
        mcp_name = str(args.get("name", "")).strip()
        if mcp_name and use_tool_needs_approval(mcp_name):
            return f"Using {mcp_name} (approval required)"
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
        self._logged_unknown_channels: set[str] = set()
        self._running = False
        # Optional zero-arg coroutine invoked once after the first successful
        # connect. Set by the launcher to run startup recovery tasks that
        # depend on the bot being live (e.g. consolidated approvals notice).
        self.on_ready: Any = None

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

    async def _send_message(self, group_id: str, text: str) -> None:
        """Send a message to a Signal group via JSON-RPC (groupId only)."""
        gid = (group_id or "").strip()
        if not gid:
            return
        chunks = _split_message(text)
        for chunk in chunks:
            await self._send_rpc("send", {"groupId": gid, "message": chunk})

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

    def _log_unknown_channel_once(self, key: str) -> None:
        if key in self._logged_unknown_channels:
            return
        self._logged_unknown_channels.add(key)
        logger.debug("signal_ignore_unknown_channel", extra={"channel": key})

    async def _handle_message(self, envelope: dict) -> None:
        """Process an incoming message envelope."""
        source, message_text, group_id_raw = _extract_message_from_envelope(envelope)
        if not source or message_text is None:
            return

        # Ignore empty messages
        content = message_text.strip() if isinstance(message_text, str) else ""
        if not content:
            return

        eng = (self.config.engagement_group_id or "").strip()
        adm = (self.config.admin_group_id or "").strip()
        group_id = _normalize_group_id(group_id_raw)

        if group_id is None:
            self._log_unknown_channel_once("__dm__")
            return

        if group_id not in (eng, adm):
            self._log_unknown_channel_once(group_id)
            return

        if group_id == adm:
            fut = self._pending_approval.get(adm)
            if fut and not fut.done():
                lc = content.lower().strip()
                if lc in ("y", "yes", "approve"):
                    fut.set_result(True)
                    return
                if lc in ("n", "no", "deny", "reject"):
                    fut.set_result(False)
                    return
            try:
                if await _handle_skill_approval_reply(self, group_id, content):
                    return
            except Exception:
                logger.exception("skill approval reply handling failed")
            fut = self._pending_approval.get(adm)
            if fut and not fut.done():
                fut.set_result(False)
                return

        is_admin_channel = group_id == adm
        session_id = _session_id_for_signal_group(group_id, admin=is_admin_channel)
        log_event(
            logger,
            "signal_message",
            session_id=session_id,
            source=source,
            group_id=group_id,
        )

        set_approval_context(group_id, self)

        async def _send_status(tool_name: str, arguments: str) -> None:
            status = _format_status(tool_name, arguments)
            try:
                await self._send_message(group_id, status)
            except Exception:
                pass

        try:
            response = await self.agent.process(content, session_id, status_callback=_send_status)
        except Exception:
            logger.exception("Agent processing failed")
            response = "Sorry, I encountered an error processing your message."

        chunks = _split_message(response)
        for chunk in chunks:
            await self._send_message(group_id, chunk)

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
        """Connect to signal-cli and run the message loop with reconnection.

        Fires ``self.on_ready`` (a coroutine-returning callable) exactly
        once — after the FIRST successful connect — so callers can deliver
        messages that require the bot to be live (e.g. the startup
        pending-approval recovery notice).
        """
        global _active_bot
        _active_bot = self
        self._running = True
        backoff = 1.0
        max_backoff = 60.0
        ready_fired = False

        while self._running:
            try:
                await self._connect()
                backoff = 1.0
                if not ready_fired and getattr(self, "on_ready", None) is not None:
                    ready_fired = True
                    try:
                        await self.on_ready()
                    except Exception:
                        logger.exception("signal_on_ready_callback_failed")
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
        global _active_bot
        if _active_bot is self:
            _active_bot = None
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
