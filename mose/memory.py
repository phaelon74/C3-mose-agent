"""Memory manager: SQLite + FTS5 + sqlite-vec for persistent, searchable memory."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlite_vec

from mose.config import MemoryConfig
from mose.observe import get_logger, log_event

logger = get_logger("memory")


def patch_nomic_extended_attention_mask(model: Any) -> int:
    """Restore ``get_extended_attention_mask`` on cached nomic-bert remote modules.

    Transformers 5 removed that helper from ``PreTrainedModel``. Older
    ``nomic-bert-2048`` ``trust_remote_code`` still calls it during encode,
    which crashes memory search and scheduled tasks.
    """
    try:
        import torch
    except ImportError:
        return 0

    def get_extended_attention_mask(self, attention_mask, input_shape, device=None, dtype=None):
        del input_shape, device
        if dtype is None:
            dtype = getattr(self, "dtype", None) or attention_mask.dtype
        if attention_mask.dim() == 3:
            extended = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            extended = attention_mask[:, None, None, :]
        else:
            raise ValueError(f"Wrong shape for attention_mask (shape {tuple(attention_mask.shape)})")
        extended = extended.to(dtype=dtype)
        return (1.0 - extended) * torch.finfo(dtype).min

    patched = 0
    seen: set[type] = set()
    modules = getattr(model, "modules", None)
    iterable = model.modules() if callable(modules) else [model]
    for module in iterable:
        cls = type(module)
        if cls in seen or "NomicBert" not in cls.__name__:
            continue
        seen.add(cls)
        if callable(getattr(cls, "get_extended_attention_mask", None)):
            continue
        cls.get_extended_attention_mask = get_extended_attention_mask
        patched += 1
    return patched

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    importance REAL DEFAULT 5.0,
    source_session TEXT,
    created_at REAL NOT NULL,
    accessed_at REAL,
    access_count INTEGER DEFAULT 0,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    msg_range_start INTEGER,
    msg_range_end INTEGER,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id);
"""

SKILL_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS skill_usage (
    id INTEGER PRIMARY KEY,
    skill_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_name ON skill_usage(skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_usage_session ON skill_usage(session_id);
"""

PENDING_APPROVALS_SQL = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    slug TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                          -- 'skill_proposal' (future: more kinds)
    recipient TEXT NOT NULL,
    proposal_path TEXT,
    payload TEXT,                                -- JSON blob with free-form context
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | rejected | expired
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_status ON pending_approvals(status);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_recipient ON pending_approvals(recipient, status);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_expires ON pending_approvals(expires_at);
"""

TRACKERS_SQL = """
CREATE TABLE IF NOT EXISTS trackers (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    collector_kind TEXT NOT NULL,
    collector_ref TEXT NOT NULL,
    schedule_seconds INTEGER NOT NULL,
    aggregations TEXT,
    alert_rules TEXT,
    recipients TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by_session TEXT,
    created_at REAL NOT NULL,
    last_run_at REAL,
    last_status TEXT,
    consecutive_failures INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tracker_samples (
    id INTEGER PRIMARY KEY,
    tracker_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (tracker_id) REFERENCES trackers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tracker_samples_tid_ts ON tracker_samples(tracker_id, ts);
CREATE TABLE IF NOT EXISTS tracker_rollups (
    tracker_id INTEGER NOT NULL,
    bucket TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    sample_id INTEGER,
    PRIMARY KEY (tracker_id, bucket, metric),
    FOREIGN KEY (tracker_id) REFERENCES trackers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tracker_alerts (
    id INTEGER PRIMARY KEY,
    tracker_id INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    triggered_at REAL NOT NULL,
    payload TEXT NOT NULL,
    notified_at REAL,
    FOREIGN KEY (tracker_id) REFERENCES trackers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tracker_alerts_tid_rule ON tracker_alerts(tracker_id, rule_id);
"""

SCHEDULED_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    recurrence TEXT NOT NULL,
    user_prompt TEXT NOT NULL,
    system_addendum TEXT,
    execution_plan TEXT NOT NULL,
    recipients TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at REAL NOT NULL,
    created_by_session TEXT,
    created_at REAL NOT NULL,
    last_run_at REAL,
    last_status TEXT,
    consecutive_failures INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled_next ON scheduled_tasks(enabled, next_run_at);
CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    summary TEXT,
    tool_trace TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_tid ON scheduled_task_runs(task_id, started_at);
"""

SCHEDULED_APPROVAL_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_approval_sessions (
    token TEXT PRIMARY KEY,
    task_slug TEXT,
    allowed_tools TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_approval_sessions_exp ON scheduled_approval_sessions(expires_at);
"""

PLAYBOOKS_SQL = """
CREATE TABLE IF NOT EXISTS playbooks (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    user_prompt_template TEXT,
    system_addendum TEXT,
    execution_plan TEXT NOT NULL,
    created_by_session TEXT,
    created_at REAL NOT NULL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_playbooks_slug ON playbooks(slug);
CREATE TABLE IF NOT EXISTS playbook_runs (
    id INTEGER PRIMARY KEY,
    playbook_id INTEGER NOT NULL,
    run_slug TEXT UNIQUE NOT NULL,
    invocation_params TEXT,
    preflight_summary TEXT,
    user_prompt TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    summary TEXT,
    tool_trace TEXT,
    FOREIGN KEY (playbook_id) REFERENCES playbooks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_playbook_runs_pid ON playbook_runs(playbook_id, started_at);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    tokenize='porter unicode61',
    content='memories',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


@dataclass
class MemoryResult:
    id: int
    content: str
    memory_type: str
    importance: float
    score: float  # combined retrieval score
    created_at: float


@dataclass
class PendingApproval:
    slug: str
    kind: str
    recipient: str
    proposal_path: str
    payload: dict[str, Any]
    created_at: float
    expires_at: float
    status: str
    decided_at: float | None = None


@dataclass
class ScheduledTaskRow:
    id: int
    slug: str
    description: str
    recurrence: dict[str, Any]
    user_prompt: str
    system_addendum: str | None
    execution_plan: dict[str, Any]
    recipients: list[str]
    enabled: bool
    next_run_at: float
    created_by_session: str | None
    created_at: float
    last_run_at: float | None
    last_status: str | None
    consecutive_failures: int


@dataclass
class ScheduledTaskRunRow:
    id: int
    task_id: int
    started_at: float
    finished_at: float | None
    status: str
    summary: str | None
    tool_trace: list[Any]


@dataclass
class PlaybookRow:
    id: int
    slug: str
    description: str
    user_prompt_template: str | None
    system_addendum: str | None
    execution_plan: dict[str, Any]
    created_by_session: str | None
    created_at: float
    updated_at: float | None


@dataclass
class PlaybookRunRow:
    id: int
    playbook_id: int
    run_slug: str
    invocation_params: dict[str, Any]
    preflight_summary: str | None
    user_prompt: str
    started_at: float
    finished_at: float | None
    status: str
    summary: str | None
    tool_trace: list[Any]


@dataclass
class TrackerRow:
    id: int
    slug: str
    description: str
    collector_kind: str
    collector_ref: str
    schedule_seconds: int
    aggregations: list[Any]
    alert_rules: list[Any]
    recipients: list[str]
    enabled: bool
    created_by_session: str | None
    created_at: float
    last_run_at: float | None
    last_status: str | None
    consecutive_failures: int


class MemoryManager:
    """Persistent memory with hybrid keyword + vector search."""

    def __init__(self, config: MemoryConfig) -> None:
        self.config = config
        self._embedder = None  # lazy load
        self._vec_initialized = False

        db_path = Path(config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.db = sqlite3.connect(str(db_path))
        self.db.enable_load_extension(True)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=1000")
        self.db.execute("PRAGMA foreign_keys=ON")
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)

        self._init_schema()
        self._ensure_skill_usage()
        self._ensure_pending_approvals()
        self._ensure_trackers()
        self._ensure_scheduled_tasks()
        self._ensure_scheduled_approval_sessions()
        self._ensure_playbooks()
        log_event(logger, "memory_initialized", db_path=config.db_path)

    def _init_schema(self) -> None:
        self.db.executescript(SCHEMA_SQL)
        self.db.executescript(FTS_SQL)

        # sqlite-vec table — check if it exists first
        exists = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_vec'"
        ).fetchone()
        if not exists:
            self.db.execute(
                f"CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[{self.config.embedding_dimensions}])"
            )
        self._vec_initialized = True
        self.db.commit()

    def _ensure_skill_usage(self) -> None:
        """Migrate older DBs that lack skill_usage."""
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_usage'"
        ).fetchone()
        if not row:
            self.db.executescript(SKILL_USAGE_SQL)
            self.db.commit()

    def _ensure_pending_approvals(self) -> None:
        """Migrate older DBs that lack pending_approvals."""
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_approvals'"
        ).fetchone()
        if not row:
            self.db.executescript(PENDING_APPROVALS_SQL)
            self.db.commit()

    def _ensure_trackers(self) -> None:
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trackers'"
        ).fetchone()
        if not row:
            self.db.executescript(TRACKERS_SQL)
            self.db.commit()

    def _ensure_scheduled_tasks(self) -> None:
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'"
        ).fetchone()
        if not row:
            self.db.executescript(SCHEDULED_TASKS_SQL)
            self.db.commit()

    def _ensure_scheduled_approval_sessions(self) -> None:
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_approval_sessions'"
        ).fetchone()
        if not row:
            self.db.executescript(SCHEDULED_APPROVAL_SESSIONS_SQL)
            self.db.commit()

    def _ensure_playbooks(self) -> None:
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='playbooks'"
        ).fetchone()
        if not row:
            self.db.executescript(PLAYBOOKS_SQL)
            self.db.commit()

    def save_scheduled_approval_session(
        self,
        token: str,
        *,
        task_slug: str,
        allowed_tools: list[str],
        ttl_seconds: float = 7200,
    ) -> None:
        """Persist an approval token so the HTTP approval bridge can bypass across processes."""
        now = time.time()
        self.purge_expired_scheduled_approval_sessions(now=now)
        self.db.execute(
            "INSERT OR REPLACE INTO scheduled_approval_sessions "
            "(token, task_slug, allowed_tools, expires_at) VALUES (?, ?, ?, ?)",
            (
                token,
                task_slug,
                json.dumps(allowed_tools),
                now + max(60.0, float(ttl_seconds)),
            ),
        )
        self.db.commit()

    def get_scheduled_approval_tools(self, token: str, *, now: float | None = None) -> frozenset[str] | None:
        ts = time.time() if now is None else now
        row = self.db.execute(
            "SELECT allowed_tools, expires_at FROM scheduled_approval_sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        allowed_raw, expires_at = row[0], float(row[1])
        if expires_at < ts:
            self.db.execute("DELETE FROM scheduled_approval_sessions WHERE token = ?", (token,))
            self.db.commit()
            return None
        try:
            parsed = json.loads(allowed_raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        tools = frozenset(str(t).strip() for t in parsed if str(t).strip())
        return tools if tools else None

    def delete_scheduled_approval_session(self, token: str) -> None:
        self.db.execute("DELETE FROM scheduled_approval_sessions WHERE token = ?", (token,))
        self.db.commit()

    def purge_expired_scheduled_approval_sessions(self, *, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        cur = self.db.execute(
            "DELETE FROM scheduled_approval_sessions WHERE expires_at < ?",
            (ts,),
        )
        self.db.commit()
        return int(cur.rowcount)

    # ---------------------------------------------------------- approvals

    def save_pending_approval(
        self,
        *,
        slug: str,
        kind: str,
        recipient: str,
        proposal_path: str,
        payload: dict[str, Any] | None,
        expires_at: float,
    ) -> None:
        """Insert or replace a pending approval row. Idempotent by ``slug``."""
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO pending_approvals "
            "(slug, kind, recipient, proposal_path, payload, created_at, expires_at, status, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL)",
            (slug, kind, recipient, proposal_path, json.dumps(payload or {}), now, expires_at),
        )
        self.db.commit()

    def get_pending_approval(self, slug: str) -> PendingApproval | None:
        row = self.db.execute(
            "SELECT slug, kind, recipient, proposal_path, payload, created_at, expires_at, status, decided_at "
            "FROM pending_approvals WHERE slug = ?",
            (slug,),
        ).fetchone()
        return self._row_to_approval(row)

    def list_pending_approvals(
        self,
        *,
        kind: str | None = None,
        recipient: str | None = None,
        status: str = "pending",
    ) -> list[PendingApproval]:
        sql = (
            "SELECT slug, kind, recipient, proposal_path, payload, created_at, expires_at, status, decided_at "
            "FROM pending_approvals WHERE status = ?"
        )
        params: list[Any] = [status]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if recipient is not None:
            sql += " AND recipient = ?"
            params.append(recipient)
        sql += " ORDER BY created_at ASC"
        rows = self.db.execute(sql, params).fetchall()
        out: list[PendingApproval] = []
        for r in rows:
            approval = self._row_to_approval(r)
            if approval is not None:
                out.append(approval)
        return out

    def expire_pending_approvals(self, *, now: float | None = None) -> list[PendingApproval]:
        """Flip any unexpired rows whose ``expires_at`` is in the past to 'expired'.

        Returns the list of rows that just transitioned — callers are responsible
        for moving their proposal files to ``skills/rejected/`` and notifying.
        """
        now = now if now is not None else time.time()
        expired = self.db.execute(
            "SELECT slug, kind, recipient, proposal_path, payload, created_at, expires_at, status, decided_at "
            "FROM pending_approvals WHERE status = 'pending' AND expires_at <= ?",
            (now,),
        ).fetchall()
        if expired:
            self.db.executemany(
                "UPDATE pending_approvals SET status = 'expired', decided_at = ? WHERE slug = ?",
                [(now, row[0]) for row in expired],
            )
            self.db.commit()
        return [a for a in (self._row_to_approval(r) for r in expired) if a is not None]

    def list_approved_approvals(
        self, *, kind: str | None = None
    ) -> list[PendingApproval]:
        """Return every row whose ``status='approved'``.

        Callers combine this with a filesystem check (is ``skills/{slug}.md``
        absent?) to detect "approved but not yet built" orphans — rows that
        were decided before the agent crashed mid-body-draft.
        """
        return self.list_pending_approvals(kind=kind, status="approved")

    def cancel_approved_approval(self, slug: str) -> PendingApproval | None:
        """Atomically flip a ``status='approved'`` row to ``'rejected'``.

        Used when an operator aborts an approved-but-unbuilt skill during
        its grace window. Returns the row (pre-transition) on success,
        ``None`` if the slug is unknown or already in some other state.
        """
        now = time.time()
        existing = self.get_pending_approval(slug)
        if existing is None or existing.status != "approved":
            return None
        cur = self.db.execute(
            "UPDATE pending_approvals SET status = 'rejected', decided_at = ? "
            "WHERE slug = ? AND status = 'approved'",
            (now, slug),
        )
        self.db.commit()
        if cur.rowcount == 0:
            return None
        return existing

    def decide_pending_approval(self, slug: str, *, approved: bool) -> PendingApproval | None:
        """Atomically transition a pending row to approved/rejected.

        Returns the row as it existed BEFORE the transition (with status still
        'pending') if the transition succeeded, else ``None`` (already decided
        or unknown slug). Callers should treat ``None`` as idempotent no-op.
        """
        now = time.time()
        existing = self.get_pending_approval(slug)
        if existing is None or existing.status != "pending":
            return None
        status = "approved" if approved else "rejected"
        cur = self.db.execute(
            "UPDATE pending_approvals SET status = ?, decided_at = ? "
            "WHERE slug = ? AND status = 'pending'",
            (status, now, slug),
        )
        self.db.commit()
        if cur.rowcount == 0:
            return None
        return existing

    @staticmethod
    def _row_to_approval(row: tuple | None) -> PendingApproval | None:
        if row is None:
            return None
        slug, kind, recipient, proposal_path, payload, created_at, expires_at, status, decided_at = row
        try:
            parsed = json.loads(payload) if payload else {}
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        return PendingApproval(
            slug=slug,
            kind=kind,
            recipient=recipient,
            proposal_path=proposal_path or "",
            payload=parsed,
            created_at=float(created_at),
            expires_at=float(expires_at),
            status=status,
            decided_at=float(decided_at) if decided_at is not None else None,
        )

    def record_skill_usage(self, skill_name: str, session_id: str, outcome: str) -> None:
        """Record whether a skill was used successfully (outcome: success|failure)."""
        self.db.execute(
            "INSERT INTO skill_usage (skill_name, session_id, outcome, created_at) VALUES (?, ?, ?, ?)",
            (skill_name, session_id, outcome, time.time()),
        )
        self.db.commit()

    def skill_failure_rates(self, limit_sessions: int = 500) -> dict[str, float]:
        """Rough failure rate per skill from recent rows (for future self-improvement)."""
        rows = self.db.execute(
            "SELECT skill_name, outcome FROM skill_usage ORDER BY id DESC LIMIT ?",
            (limit_sessions,),
        ).fetchall()
        counts: dict[str, list[int]] = {}
        for name, out in rows:
            if name not in counts:
                counts[name] = [0, 0]
            counts[name][0] += 1
            if out == "failure":
                counts[name][1] += 1
        return {k: v[1] / v[0] if v[0] else 0.0 for k, v in counts.items()}

    def skill_usage_counts(self, limit_sessions: int = 500) -> dict[str, int]:
        """Return total usage count per skill over the most recent ``limit_sessions`` rows."""
        rows = self.db.execute(
            "SELECT skill_name, COUNT(*) FROM ("
            "  SELECT skill_name FROM skill_usage ORDER BY id DESC LIMIT ?"
            ") GROUP BY skill_name",
            (limit_sessions,),
        ).fetchall()
        return {name: int(count) for name, count in rows}

    @property
    def embedder(self):
        """Lazy-load the embedding model on first use."""
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer

            kwargs = {
                "truncate_dim": self.config.embedding_dimensions,
                "device": "cpu",
            }
            try:
                self._embedder = SentenceTransformer(
                    self.config.embedding_model,
                    trust_remote_code=False,
                    **kwargs,
                )
                log_event(
                    logger,
                    "embedder_loaded",
                    model=self.config.embedding_model,
                    trust_remote_code=False,
                )
            except Exception as e:
                log_event(
                    logger,
                    "embedder_native_load_failed",
                    error=str(e)[:400],
                    model=self.config.embedding_model,
                )
                self._embedder = SentenceTransformer(
                    self.config.embedding_model,
                    trust_remote_code=True,
                    **kwargs,
                )
                log_event(
                    logger,
                    "embedder_loaded",
                    model=self.config.embedding_model,
                    trust_remote_code=True,
                )
            n = patch_nomic_extended_attention_mask(self._embedder)
            if n:
                log_event(logger, "nomic_attention_mask_patched", modules=n)
        return self._embedder

    def _embed(self, text: str) -> list[float]:
        """Generate embedding for a text string."""
        # nomic-embed-text requires "search_query: " or "search_document: " prefix
        vec = self.embedder.encode(f"search_query: {text}", normalize_embeddings=True)
        return vec.tolist()

    def _embed_document(self, text: str) -> list[float]:
        """Generate embedding for a document to be stored."""
        vec = self.embedder.encode(f"search_document: {text}", normalize_embeddings=True)
        return vec.tolist()

    # --- Message History ---

    def save_message(self, session_id: str, role: str, content: str, tool_calls: list | None = None) -> int:
        now = time.time()
        tc_json = json.dumps(tool_calls) if tool_calls else None
        cur = self.db.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, tc_json, now),
        )
        self.db.commit()
        log_event(logger, "message_saved", session_id=session_id, role=role, msg_id=cur.lastrowid)
        return cur.lastrowid

    def get_recent_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get the most recent messages for a session, formatted for the LLM."""
        rows = self.db.execute(
            "SELECT role, content, tool_calls FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()

        messages = []
        for role, content, tc_json in reversed(rows):
            msg: dict[str, Any] = {"role": role, "content": content}
            if tc_json:
                msg["tool_calls"] = json.loads(tc_json)
            messages.append(msg)
        return messages

    def get_message_count(self, session_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0]

    # --- Memory CRUD ---

    def store_memory(
        self,
        content: str,
        memory_type: str = "fact",
        importance: float = 5.0,
        source_session: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Store a new memory with embedding."""
        now = time.time()
        meta_json = json.dumps(metadata) if metadata else None
        cur = self.db.execute(
            "INSERT INTO memories (content, memory_type, importance, source_session, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, memory_type, importance, source_session, now, meta_json),
        )
        mem_id = cur.lastrowid

        try:
            embedding = self._embed_document(content)
            self.db.execute(
                "INSERT INTO memories_vec (rowid, embedding) VALUES (?, ?)",
                (mem_id, json.dumps(embedding)),
            )
        except Exception:
            logger.exception("memory_embed_store_failed", extra={"mem_id": mem_id})

        self.db.commit()
        log_event(logger, "memory_stored", mem_id=mem_id, memory_type=memory_type, importance=importance)
        return mem_id

    # --- Search ---

    def _fts_search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        """Full-text search, returns (id, rank) pairs."""
        rows = self.db.execute(
            "SELECT rowid, rank FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def _vec_search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        """Vector similarity search, returns (id, distance) pairs."""
        embedding = self._embed(query)
        rows = self.db.execute(
            "SELECT rowid, distance FROM memories_vec "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (json.dumps(embedding), limit),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def search(self, query: str, top_k: int | None = None) -> list[MemoryResult]:
        """Hybrid search with Reciprocal Rank Fusion."""
        if top_k is None:
            top_k = self.config.top_k

        # Get results from both search methods
        try:
            fts_results = self._fts_search(query)
        except sqlite3.OperationalError:
            fts_results = []

        try:
            vec_results = self._vec_search(query)
        except Exception:
            logger.exception("memory_vec_search_failed")
            vec_results = []

        # RRF: score = sum(1 / (k + rank)) across methods
        k = self.config.rrf_k
        scores: dict[int, float] = {}

        for rank, (mem_id, _) in enumerate(fts_results):
            scores[mem_id] = scores.get(mem_id, 0) + 1.0 / (k + rank + 1)

        for rank, (mem_id, _) in enumerate(vec_results):
            scores[mem_id] = scores.get(mem_id, 0) + 1.0 / (k + rank + 1)

        if not scores:
            return []

        # Fetch memory details and apply recency/importance weighting
        mem_ids = list(scores.keys())
        placeholders = ",".join("?" * len(mem_ids))
        rows = self.db.execute(
            f"SELECT id, content, memory_type, importance, created_at FROM memories WHERE id IN ({placeholders})",
            mem_ids,
        ).fetchall()

        now = time.time()
        results = []
        for row in rows:
            mem_id, content, mtype, importance, created_at = row
            base_score = scores[mem_id]

            # Recency boost: exponential decay, halves every 7 days
            age_days = (now - created_at) / 86400
            recency = 2 ** (-age_days / 7)
            final_score = base_score + self.config.recency_weight * recency

            # Importance boost (normalized)
            final_score += (importance / 10.0) * 0.1

            results.append(MemoryResult(
                id=mem_id,
                content=content,
                memory_type=mtype,
                importance=importance,
                score=final_score,
                created_at=created_at,
            ))

            # Update access tracking
            self.db.execute(
                "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                (now, mem_id),
            )

        self.db.commit()
        results.sort(key=lambda r: r.score, reverse=True)

        log_event(logger, "memory_search", query_len=len(query), fts_hits=len(fts_results),
                  vec_hits=len(vec_results), returned=min(top_k, len(results)))

        return results[:top_k]

    # --- Session Summaries ---

    def get_session_summary(self, session_id: str) -> str | None:
        """Get the most recent summary for a session."""
        row = self.db.execute(
            "SELECT summary FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row[0] if row else None

    def store_summary(self, session_id: str, summary: str, msg_start: int, msg_end: int) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO summaries (session_id, summary, msg_range_start, msg_range_end, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, summary, msg_start, msg_end, now),
        )
        self.db.commit()
        log_event(logger, "summary_stored", session_id=session_id, msg_range=f"{msg_start}-{msg_end}")
        return cur.lastrowid

    def should_summarize(self, session_id: str) -> bool:
        """Check if enough unsummarized messages have accumulated."""
        # Find the last summarized message ID
        row = self.db.execute(
            "SELECT COALESCE(MAX(msg_range_end), 0) FROM summaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        last_summarized = row[0]

        # Count messages since then
        row = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id > ?",
            (session_id, last_summarized),
        ).fetchone()
        return row[0] >= self.config.summary_interval

    async def summarize_and_extract(self, session_id: str, llm) -> None:
        """Summarize recent messages and extract facts. Called periodically."""
        # Get unsummarized messages
        row = self.db.execute(
            "SELECT COALESCE(MAX(msg_range_end), 0) FROM summaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        last_summarized = row[0]

        rows = self.db.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? AND id > ? ORDER BY id",
            (session_id, last_summarized),
        ).fetchall()

        if not rows:
            return

        msg_start = rows[0][0]
        msg_end = rows[-1][0]
        conversation = "\n".join(f"{role}: {content}" for _, role, content in rows)

        from mose.context_compress import compress_text_if_needed, max_input_tokens

        budget = max_input_tokens()
        conversation = await compress_text_if_needed(
            conversation,
            llm=llm,
            query_context="session memory extraction",
            max_output_tokens=max(2048, budget // 4),
            source=f"memory_{session_id}",
        )

        # Ask LLM to summarize and extract facts
        extract_prompt = [
            {"role": "system", "content": (
                "You are a memory extraction system. Given a conversation, do two things:\n"
                "1. Write a brief summary (2-3 sentences) of what was discussed.\n"
                "2. Extract key facts as a JSON array of objects with 'content' (the fact) and "
                "'importance' (1-10, where 10 is critical).\n\n"
                "Respond in this exact JSON format:\n"
                '{"summary": "...", "facts": [{"content": "...", "importance": 5}, ...]}'
            )},
            {"role": "user", "content": f"Extract from this conversation:\n\n{conversation}"},
        ]

        try:
            response = await llm.chat(extract_prompt)
            data = json.loads(response.content)

            # Store summary
            self.store_summary(session_id, data["summary"], msg_start, msg_end)

            # Store extracted facts
            for fact in data.get("facts", []):
                if fact.get("importance", 0) >= self.config.importance_threshold:
                    self.store_memory(
                        content=fact["content"],
                        memory_type="fact",
                        importance=fact["importance"],
                        source_session=session_id,
                    )

            log_event(logger, "extraction_complete", session_id=session_id,
                      facts_extracted=len(data.get("facts", [])))
        except Exception:
            logger.exception("Failed to summarize/extract")

    # --- Trackers (scheduled data collection) ---

    @staticmethod
    def _parse_json_list(raw: str | None) -> list[Any]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _parse_json_str_list(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x) for x in data]
            return []
        except (json.JSONDecodeError, TypeError):
            return []

    def _row_to_tracker(self, row: tuple[Any, ...] | None) -> TrackerRow | None:
        if row is None:
            return None
        (
            tid,
            slug,
            description,
            collector_kind,
            collector_ref,
            schedule_seconds,
            aggregations,
            alert_rules,
            recipients,
            enabled,
            created_by_session,
            created_at,
            last_run_at,
            last_status,
            consecutive_failures,
        ) = row
        return TrackerRow(
            id=int(tid),
            slug=str(slug),
            description=str(description),
            collector_kind=str(collector_kind),
            collector_ref=str(collector_ref),
            schedule_seconds=int(schedule_seconds),
            aggregations=self._parse_json_list(aggregations),
            alert_rules=self._parse_json_list(alert_rules),
            recipients=self._parse_json_str_list(recipients),
            enabled=bool(enabled),
            created_by_session=created_by_session,
            created_at=float(created_at),
            last_run_at=float(last_run_at) if last_run_at is not None else None,
            last_status=str(last_status) if last_status is not None else None,
            consecutive_failures=int(consecutive_failures or 0),
        )

    def create_tracker(
        self,
        *,
        slug: str,
        description: str,
        collector_kind: str,
        collector_ref: str,
        schedule_seconds: int,
        aggregations: list[Any] | None = None,
        alert_rules: list[Any] | None = None,
        recipients: list[str] | None = None,
        created_by_session: str | None = None,
        enabled: bool = True,
    ) -> int:
        now = time.time()
        agg_json = json.dumps(aggregations or [])
        rules_json = json.dumps(alert_rules or [])
        rec_json = json.dumps(recipients or ["signal:admin"])
        cur = self.db.execute(
            "INSERT INTO trackers (slug, description, collector_kind, collector_ref, "
            "schedule_seconds, aggregations, alert_rules, recipients, enabled, "
            "created_by_session, created_at, last_run_at, last_status, consecutive_failures) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
            (
                slug,
                description,
                collector_kind,
                collector_ref,
                schedule_seconds,
                agg_json,
                rules_json,
                rec_json,
                1 if enabled else 0,
                created_by_session,
                now,
            ),
        )
        self.db.commit()
        log_event(logger, "tracker_created", slug=slug, tracker_id=cur.lastrowid)
        return int(cur.lastrowid)

    def update_tracker(self, slug: str, **fields: Any) -> bool:
        if not fields:
            return False
        allowed = {
            "description",
            "collector_kind",
            "collector_ref",
            "schedule_seconds",
            "aggregations",
            "alert_rules",
            "recipients",
            "enabled",
            "last_run_at",
            "last_status",
            "consecutive_failures",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("aggregations", "alert_rules"):
                v = json.dumps(v if v is not None else [])
            elif k == "recipients":
                v = json.dumps(v if v is not None else [])
            elif k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return False
        vals.append(slug)
        cur = self.db.execute(
            f"UPDATE trackers SET {', '.join(sets)} WHERE slug = ?",
            vals,
        )
        self.db.commit()
        return cur.rowcount > 0

    def delete_tracker(self, slug: str) -> bool:
        cur = self.db.execute("DELETE FROM trackers WHERE slug = ?", (slug,))
        self.db.commit()
        return cur.rowcount > 0

    def list_trackers(self, *, enabled_only: bool = False) -> list[TrackerRow]:
        sql = (
            "SELECT id, slug, description, collector_kind, collector_ref, schedule_seconds, "
            "aggregations, alert_rules, recipients, enabled, created_by_session, created_at, "
            "last_run_at, last_status, consecutive_failures FROM trackers"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY slug"
        rows = self.db.execute(sql).fetchall()
        return [t for t in (self._row_to_tracker(r) for r in rows) if t is not None]

    def list_trackers_degraded(self) -> list[TrackerRow]:
        rows = self.db.execute(
            "SELECT id, slug, description, collector_kind, collector_ref, schedule_seconds, "
            "aggregations, alert_rules, recipients, enabled, created_by_session, created_at, "
            "last_run_at, last_status, consecutive_failures FROM trackers "
            "WHERE consecutive_failures > 0 ORDER BY slug"
        ).fetchall()
        return [t for t in (self._row_to_tracker(r) for r in rows) if t is not None]

    def get_tracker(self, slug: str) -> TrackerRow | None:
        row = self.db.execute(
            "SELECT id, slug, description, collector_kind, collector_ref, schedule_seconds, "
            "aggregations, alert_rules, recipients, enabled, created_by_session, created_at, "
            "last_run_at, last_status, consecutive_failures FROM trackers WHERE slug = ?",
            (slug,),
        ).fetchone()
        return self._row_to_tracker(row)

    def insert_tracker_sample(self, tracker_id: int, ts: float, payload: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO tracker_samples (tracker_id, ts, payload) VALUES (?, ?, ?)",
            (tracker_id, ts, json.dumps(payload)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def upsert_tracker_rollup(
        self,
        tracker_id: int,
        bucket: str,
        metric: str,
        value: float,
        sample_id: int,
    ) -> tuple[float | None, float]:
        row = self.db.execute(
            "SELECT value, sample_id FROM tracker_rollups "
            "WHERE tracker_id = ? AND bucket = ? AND metric = ?",
            (tracker_id, bucket, metric),
        ).fetchone()
        prev = float(row[0]) if row else None
        if prev is None:
            new_val = value
            self.db.execute(
                "INSERT INTO tracker_rollups (tracker_id, bucket, metric, value, sample_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (tracker_id, bucket, metric, new_val, sample_id),
            )
        else:
            new_val = max(prev, value)
            self.db.execute(
                "UPDATE tracker_rollups SET value = ?, sample_id = ? "
                "WHERE tracker_id = ? AND bucket = ? AND metric = ?",
                (new_val, sample_id, tracker_id, bucket, metric),
            )
        self.db.commit()
        return (prev, new_val)

    def max_tracker_rollup_in_range(
        self,
        tracker_id: int,
        metric: str,
        *,
        min_bucket: str,
        max_bucket_exclusive: str,
    ) -> float | None:
        """Maximum rollup value for buckets in [min_bucket, max_bucket_exclusive)."""
        row = self.db.execute(
            "SELECT MAX(value) FROM tracker_rollups WHERE tracker_id = ? AND metric = ? "
            "AND bucket >= ? AND bucket < ?",
            (tracker_id, metric, min_bucket, max_bucket_exclusive),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def query_tracker_samples(
        self,
        slug: str,
        *,
        since: float | None = None,
        until: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        tr = self.get_tracker(slug)
        if tr is None:
            return []
        sql = "SELECT id, ts, payload FROM tracker_samples WHERE tracker_id = ?"
        params: list[Any] = [tr.id]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        out: list[dict[str, Any]] = []
        for sid, ts, payload in self.db.execute(sql, params).fetchall():
            try:
                pl = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                pl = {}
            out.append({"id": sid, "ts": ts, "payload": pl})
        return out

    def query_tracker_rollups(
        self,
        slug: str,
        *,
        metric: str | None = None,
        since_bucket: str | None = None,
        until_bucket: str | None = None,
    ) -> list[dict[str, Any]]:
        tr = self.get_tracker(slug)
        if tr is None:
            return []
        sql = "SELECT bucket, metric, value, sample_id FROM tracker_rollups WHERE tracker_id = ?"
        params: list[Any] = [tr.id]
        if metric:
            sql += " AND metric = ?"
            params.append(metric)
        if since_bucket:
            sql += " AND bucket >= ?"
            params.append(since_bucket)
        if until_bucket:
            sql += " AND bucket <= ?"
            params.append(until_bucket)
        sql += " ORDER BY bucket, metric"
        return [
            {"bucket": r[0], "metric": r[1], "value": r[2], "sample_id": r[3]}
            for r in self.db.execute(sql, params).fetchall()
        ]

    def query_tracker_stats(
        self,
        slug: str,
        *,
        since: float | None = None,
        until: float | None = None,
        metrics: list[str] | None = None,
    ) -> dict[str, Any]:
        """Aggregate min/max/avg/count for metrics over a sample time range."""
        tr = self.get_tracker(slug)
        if tr is None:
            return {"error": f"unknown tracker '{slug}'"}

        metric_names: list[str] = list(metrics) if metrics else []
        if not metric_names:
            for item in tr.aggregations or []:
                if isinstance(item, str):
                    metric_names.append(item)
                elif isinstance(item, dict) and item.get("metric"):
                    metric_names.append(str(item["metric"]))

        base_where = "tracker_id = ?"
        params: list[Any] = [tr.id]
        if since is not None:
            base_where += " AND ts >= ?"
            params.append(since)
        if until is not None:
            base_where += " AND ts <= ?"
            params.append(until)

        count_row = self.db.execute(
            f"SELECT COUNT(*) FROM tracker_samples WHERE {base_where}",
            params,
        ).fetchone()
        sample_count = int(count_row[0]) if count_row else 0

        out_metrics: dict[str, Any] = {}
        for m in metric_names:
            path = f"$.metrics.{m}"
            agg = self.db.execute(
                f"SELECT MAX(CAST(json_extract(payload, ?) AS REAL)), "
                f"MIN(CAST(json_extract(payload, ?) AS REAL)), "
                f"AVG(CAST(json_extract(payload, ?) AS REAL)), "
                f"COUNT(json_extract(payload, ?)) "
                f"FROM tracker_samples WHERE {base_where} "
                f"AND json_extract(payload, ?) IS NOT NULL",
                [path, path, path, path, *params, path],
            ).fetchone()
            max_row = self.db.execute(
                f"SELECT id, ts FROM tracker_samples WHERE {base_where} "
                f"AND json_extract(payload, ?) IS NOT NULL "
                f"ORDER BY CAST(json_extract(payload, ?) AS REAL) DESC LIMIT 1",
                [*params, path, path],
            ).fetchone()
            out_metrics[m] = {
                "max": float(agg[0]) if agg and agg[0] is not None else None,
                "min": float(agg[1]) if agg and agg[1] is not None else None,
                "avg": round(float(agg[2]), 4) if agg and agg[2] is not None else None,
                "count": int(agg[3]) if agg and agg[3] is not None else 0,
                "max_sample_id": int(max_row[0]) if max_row else None,
                "max_ts": float(max_row[1]) if max_row else None,
            }

        return {
            "slug": slug,
            "since": since,
            "until": until,
            "sample_count": sample_count,
            "metrics": out_metrics,
        }

    def record_tracker_alert(
        self,
        tracker_id: int,
        rule_id: str,
        payload: dict[str, Any],
    ) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO tracker_alerts (tracker_id, rule_id, triggered_at, payload, notified_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (tracker_id, rule_id, now, json.dumps(payload)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def mark_tracker_alert_notified(self, alert_id: int) -> None:
        now = time.time()
        self.db.execute(
            "UPDATE tracker_alerts SET notified_at = ? WHERE id = ?",
            (now, alert_id),
        )
        self.db.commit()

    def tracker_alert_exists_for_day(
        self,
        tracker_id: int,
        rule_id: str,
        day_bucket: str,
    ) -> bool:
        """Dedupe: same rule + calendar day in payload['day_bucket']."""
        rows = self.db.execute(
            "SELECT payload FROM tracker_alerts WHERE tracker_id = ? AND rule_id = ?",
            (tracker_id, rule_id),
        ).fetchall()
        for (raw,) in rows:
            try:
                d = json.loads(raw)
                if isinstance(d, dict) and d.get("day_bucket") == day_bucket:
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    @staticmethod
    def utc_day_bucket(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def min_bucket_for_lookback(day_bucket: str, days: int) -> str:
        d = datetime.strptime(day_bucket, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (d - timedelta(days=max(0, days))).strftime("%Y-%m-%d")

    def compact_tracker_storage(
        self,
        *,
        sample_retention_days: int,
        rollup_retention_days: int,
        now: float | None = None,
        vacuum: bool = False,
    ) -> dict[str, int]:
        now = now if now is not None else time.time()
        cutoff_ts = now - max(0, sample_retention_days) * 86400
        cur_s = self.db.execute("DELETE FROM tracker_samples WHERE ts < ?", (cutoff_ts,))
        deleted_samples = cur_s.rowcount

        day = datetime.fromtimestamp(now, tz=timezone.utc).date()
        rollup_cutoff = day - timedelta(days=max(0, rollup_retention_days))
        cutoff_bucket = rollup_cutoff.strftime("%Y-%m-%d")
        cur_r = self.db.execute("DELETE FROM tracker_rollups WHERE bucket < ?", (cutoff_bucket,))
        deleted_rollups = cur_r.rowcount

        old_alerts = now - max(0, rollup_retention_days) * 86400 * 2
        cur_a = self.db.execute("DELETE FROM tracker_alerts WHERE triggered_at < ?", (old_alerts,))
        deleted_alerts = cur_a.rowcount

        self.db.commit()
        if vacuum:
            self.db.execute("VACUUM")
            self.db.commit()
        return {
            "deleted_samples": deleted_samples,
            "deleted_rollups": deleted_rollups,
            "deleted_alerts": deleted_alerts,
        }

    # ---------------------------------------------------------- scheduled tasks

    def _parse_json_dict(self, raw: Any) -> dict[str, Any]:
        if raw is None or raw == "":
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return {}

    def _row_to_scheduled_task(self, row: tuple[Any, ...] | None) -> ScheduledTaskRow | None:
        if row is None:
            return None
        (
            tid,
            slug,
            description,
            recurrence,
            user_prompt,
            system_addendum,
            execution_plan,
            recipients,
            enabled,
            next_run_at,
            created_by_session,
            created_at,
            last_run_at,
            last_status,
            consecutive_failures,
        ) = row
        return ScheduledTaskRow(
            id=int(tid),
            slug=str(slug),
            description=str(description),
            recurrence=self._parse_json_dict(recurrence),
            user_prompt=str(user_prompt),
            system_addendum=str(system_addendum) if system_addendum else None,
            execution_plan=self._parse_json_dict(execution_plan),
            recipients=self._parse_json_str_list(recipients),
            enabled=bool(enabled),
            next_run_at=float(next_run_at),
            created_by_session=created_by_session,
            created_at=float(created_at),
            last_run_at=float(last_run_at) if last_run_at is not None else None,
            last_status=str(last_status) if last_status is not None else None,
            consecutive_failures=int(consecutive_failures or 0),
        )

    def _row_to_scheduled_task_run(self, row: tuple[Any, ...] | None) -> ScheduledTaskRunRow | None:
        if row is None:
            return None
        rid, task_id, started_at, finished_at, status, summary, tool_trace = row
        trace = self._parse_json_list(tool_trace)
        return ScheduledTaskRunRow(
            id=int(rid),
            task_id=int(task_id),
            started_at=float(started_at),
            finished_at=float(finished_at) if finished_at is not None else None,
            status=str(status),
            summary=str(summary) if summary is not None else None,
            tool_trace=trace,
        )

    def create_scheduled_task(
        self,
        *,
        slug: str,
        description: str,
        recurrence: dict[str, Any],
        user_prompt: str,
        system_addendum: str | None = None,
        execution_plan: dict[str, Any],
        recipients: list[str] | None = None,
        next_run_at: float,
        created_by_session: str | None = None,
        enabled: bool = True,
    ) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO scheduled_tasks (slug, description, recurrence, user_prompt, "
            "system_addendum, execution_plan, recipients, enabled, next_run_at, "
            "created_by_session, created_at, last_run_at, last_status, consecutive_failures) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
            (
                slug,
                description,
                json.dumps(recurrence),
                user_prompt,
                system_addendum,
                json.dumps(execution_plan),
                json.dumps(recipients or ["signal:admin"]),
                1 if enabled else 0,
                next_run_at,
                created_by_session,
                now,
            ),
        )
        self.db.commit()
        log_event(logger, "scheduled_task_created", slug=slug, task_id=cur.lastrowid)
        return int(cur.lastrowid)

    def update_scheduled_task(self, slug: str, **fields: Any) -> bool:
        if not fields:
            return False
        allowed = {
            "description",
            "recurrence",
            "user_prompt",
            "system_addendum",
            "execution_plan",
            "recipients",
            "enabled",
            "next_run_at",
            "last_run_at",
            "last_status",
            "consecutive_failures",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("recurrence", "execution_plan"):
                v = json.dumps(v if v is not None else {})
            elif k == "recipients":
                v = json.dumps(v if v is not None else [])
            elif k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return False
        vals.append(slug)
        cur = self.db.execute(
            f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE slug = ?",
            vals,
        )
        self.db.commit()
        return cur.rowcount > 0

    def delete_scheduled_task(self, slug: str) -> bool:
        cur = self.db.execute("DELETE FROM scheduled_tasks WHERE slug = ?", (slug,))
        self.db.commit()
        return cur.rowcount > 0

    def get_scheduled_task(self, slug: str) -> ScheduledTaskRow | None:
        row = self.db.execute(
            "SELECT id, slug, description, recurrence, user_prompt, system_addendum, "
            "execution_plan, recipients, enabled, next_run_at, created_by_session, created_at, "
            "last_run_at, last_status, consecutive_failures FROM scheduled_tasks WHERE slug = ?",
            (slug,),
        ).fetchone()
        return self._row_to_scheduled_task(row)

    def list_scheduled_tasks(self, *, enabled_only: bool = False) -> list[ScheduledTaskRow]:
        sql = (
            "SELECT id, slug, description, recurrence, user_prompt, system_addendum, "
            "execution_plan, recipients, enabled, next_run_at, created_by_session, created_at, "
            "last_run_at, last_status, consecutive_failures FROM scheduled_tasks"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY slug"
        rows = self.db.execute(sql).fetchall()
        return [t for t in (self._row_to_scheduled_task(r) for r in rows) if t is not None]

    def list_due_scheduled_tasks(self, *, now: float | None = None) -> list[ScheduledTaskRow]:
        now = now if now is not None else time.time()
        rows = self.db.execute(
            "SELECT id, slug, description, recurrence, user_prompt, system_addendum, "
            "execution_plan, recipients, enabled, next_run_at, created_by_session, created_at, "
            "last_run_at, last_status, consecutive_failures FROM scheduled_tasks "
            "WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC",
            (now,),
        ).fetchall()
        return [t for t in (self._row_to_scheduled_task(r) for r in rows) if t is not None]

    def list_scheduled_tasks_degraded(self) -> list[ScheduledTaskRow]:
        rows = self.db.execute(
            "SELECT id, slug, description, recurrence, user_prompt, system_addendum, "
            "execution_plan, recipients, enabled, next_run_at, created_by_session, created_at, "
            "last_run_at, last_status, consecutive_failures FROM scheduled_tasks "
            "WHERE consecutive_failures > 0 ORDER BY slug"
        ).fetchall()
        return [t for t in (self._row_to_scheduled_task(r) for r in rows) if t is not None]

    def insert_scheduled_task_run(
        self,
        task_id: int,
        *,
        started_at: float,
        status: str,
        finished_at: float | None = None,
        summary: str | None = None,
        tool_trace: list[Any] | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO scheduled_task_runs "
            "(task_id, started_at, finished_at, status, summary, tool_trace) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                started_at,
                finished_at,
                status,
                summary,
                json.dumps(tool_trace or []),
            ),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def update_scheduled_task_run(
        self,
        run_id: int,
        *,
        finished_at: float | None = None,
        status: str | None = None,
        summary: str | None = None,
        tool_trace: list[Any] | None = None,
    ) -> bool:
        fields: dict[str, Any] = {}
        if finished_at is not None:
            fields["finished_at"] = finished_at
        if status is not None:
            fields["status"] = status
        if summary is not None:
            fields["summary"] = summary
        if tool_trace is not None:
            fields["tool_trace"] = json.dumps(tool_trace)
        if not fields:
            return False
        sets = [f"{k} = ?" for k in fields]
        vals = list(fields.values()) + [run_id]
        cur = self.db.execute(
            f"UPDATE scheduled_task_runs SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        self.db.commit()
        return cur.rowcount > 0

    def compact_scheduled_task_runs(
        self,
        *,
        retention_days: int,
        now: float | None = None,
    ) -> int:
        now = now if now is not None else time.time()
        cutoff = now - max(0, retention_days) * 86400
        cur = self.db.execute(
            "DELETE FROM scheduled_task_runs WHERE started_at < ?",
            (cutoff,),
        )
        self.db.commit()
        return int(cur.rowcount)

    def _row_to_playbook(self, row: tuple[Any, ...] | None) -> PlaybookRow | None:
        if row is None:
            return None
        (
            pid,
            slug,
            description,
            user_prompt_template,
            system_addendum,
            execution_plan,
            created_by_session,
            created_at,
            updated_at,
        ) = row
        return PlaybookRow(
            id=int(pid),
            slug=str(slug),
            description=str(description),
            user_prompt_template=str(user_prompt_template) if user_prompt_template else None,
            system_addendum=str(system_addendum) if system_addendum else None,
            execution_plan=self._parse_json_dict(execution_plan),
            created_by_session=created_by_session,
            created_at=float(created_at),
            updated_at=float(updated_at) if updated_at is not None else None,
        )

    def _row_to_playbook_run(self, row: tuple[Any, ...] | None) -> PlaybookRunRow | None:
        if row is None:
            return None
        (
            rid,
            playbook_id,
            run_slug,
            invocation_params,
            preflight_summary,
            user_prompt,
            started_at,
            finished_at,
            status,
            summary,
            tool_trace,
        ) = row
        return PlaybookRunRow(
            id=int(rid),
            playbook_id=int(playbook_id),
            run_slug=str(run_slug),
            invocation_params=self._parse_json_dict(invocation_params),
            preflight_summary=str(preflight_summary) if preflight_summary else None,
            user_prompt=str(user_prompt),
            started_at=float(started_at),
            finished_at=float(finished_at) if finished_at is not None else None,
            status=str(status),
            summary=str(summary) if summary is not None else None,
            tool_trace=self._parse_json_list(tool_trace),
        )

    def create_playbook(
        self,
        *,
        slug: str,
        description: str,
        execution_plan: dict[str, Any],
        user_prompt_template: str | None = None,
        system_addendum: str | None = None,
        created_by_session: str | None = None,
    ) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO playbooks (slug, description, user_prompt_template, system_addendum, "
            "execution_plan, created_by_session, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                slug,
                description,
                user_prompt_template,
                system_addendum,
                json.dumps(execution_plan),
                created_by_session,
                now,
                now,
            ),
        )
        self.db.commit()
        log_event(logger, "playbook_created", slug=slug, playbook_id=cur.lastrowid)
        return int(cur.lastrowid)

    def update_playbook(self, slug: str, **fields: Any) -> bool:
        if not fields:
            return False
        allowed = {
            "description",
            "user_prompt_template",
            "system_addendum",
            "execution_plan",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "execution_plan":
                v = json.dumps(v if v is not None else {})
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(slug)
        cur = self.db.execute(
            f"UPDATE playbooks SET {', '.join(sets)} WHERE slug = ?",
            vals,
        )
        self.db.commit()
        return cur.rowcount > 0

    def delete_playbook(self, slug: str) -> bool:
        cur = self.db.execute("DELETE FROM playbooks WHERE slug = ?", (slug,))
        self.db.commit()
        return cur.rowcount > 0

    def get_playbook(self, slug: str) -> PlaybookRow | None:
        row = self.db.execute(
            "SELECT id, slug, description, user_prompt_template, system_addendum, "
            "execution_plan, created_by_session, created_at, updated_at "
            "FROM playbooks WHERE slug = ?",
            (slug,),
        ).fetchone()
        return self._row_to_playbook(row)

    def list_playbooks(self) -> list[PlaybookRow]:
        rows = self.db.execute(
            "SELECT id, slug, description, user_prompt_template, system_addendum, "
            "execution_plan, created_by_session, created_at, updated_at "
            "FROM playbooks ORDER BY slug"
        ).fetchall()
        return [p for p in (self._row_to_playbook(r) for r in rows) if p is not None]

    def insert_playbook_run(
        self,
        playbook_id: int,
        *,
        run_slug: str,
        user_prompt: str,
        invocation_params: dict[str, Any] | None = None,
        preflight_summary: str | None = None,
        started_at: float,
        status: str,
        finished_at: float | None = None,
        summary: str | None = None,
        tool_trace: list[Any] | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO playbook_runs "
            "(playbook_id, run_slug, invocation_params, preflight_summary, user_prompt, "
            "started_at, finished_at, status, summary, tool_trace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                playbook_id,
                run_slug,
                json.dumps(invocation_params or {}),
                preflight_summary,
                user_prompt,
                started_at,
                finished_at,
                status,
                summary,
                json.dumps(tool_trace or []),
            ),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def update_playbook_run(
        self,
        run_id: int,
        *,
        finished_at: float | None = None,
        status: str | None = None,
        summary: str | None = None,
        tool_trace: list[Any] | None = None,
    ) -> bool:
        fields: dict[str, Any] = {}
        if finished_at is not None:
            fields["finished_at"] = finished_at
        if status is not None:
            fields["status"] = status
        if summary is not None:
            fields["summary"] = summary
        if tool_trace is not None:
            fields["tool_trace"] = json.dumps(tool_trace)
        if not fields:
            return False
        sets = [f"{k} = ?" for k in fields]
        vals = list(fields.values()) + [run_id]
        cur = self.db.execute(
            f"UPDATE playbook_runs SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        self.db.commit()
        return cur.rowcount > 0

    def get_playbook_run(self, run_slug: str) -> PlaybookRunRow | None:
        row = self.db.execute(
            "SELECT id, playbook_id, run_slug, invocation_params, preflight_summary, "
            "user_prompt, started_at, finished_at, status, summary, tool_trace "
            "FROM playbook_runs WHERE run_slug = ?",
            (run_slug,),
        ).fetchone()
        return self._row_to_playbook_run(row)

    def close(self) -> None:
        self.db.close()
