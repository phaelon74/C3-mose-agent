"""Load configuration from config.toml with env var overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMConfig:
    endpoint: str = "http://localhost:8001/v1"
    model: str = "worker-agent"
    max_tokens: int = 16384
    temperature: float = 1.0
    context_window: int = 98304
    # TabbyAPI and many OpenAI-compatible servers require Bearer auth; empty = no key (local vLLM).
    api_key: str = ""
    provider: str = "openai_compat"  # openai_compat | tabby | vllm | bedrock


@dataclass
class DiscordConfig:
    token: str = ""


@dataclass
class SignalConfig:
    phone_number: str = ""
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 7583
    # Proactive messages (skill proposals, skill review summaries, alerts) are
    # sent to this recipient. If unset, proactive notifications are skipped.
    admin_recipient: str = ""
    # Seconds to wait for a human response on a skill proposal (12 hours).
    proposal_timeout_seconds: int = 43200


@dataclass
class MemoryConfig:
    db_path: str = "data/memory.db"
    embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embedding_dimensions: int = 384
    top_k: int = 10
    chunk_size: int = 500
    summary_interval: int = 50
    rrf_k: int = 60
    importance_threshold: float = 3.0
    recency_weight: float = 0.3


@dataclass
class ObserveConfig:
    log_dir: str = "data/logs"
    log_level: str = "INFO"
    web_dashboard: bool = False
    web_port: int = 8900


@dataclass
class AgentConfig:
    workspace: str = "data/workspace"
    allow_read_outside: bool = True
    skills_path: str = "skills"
    recent_messages_limit: int = 15


@dataclass
class TerminalConfig:
    """Where shell tools run: local bash argv, or docker exec into a sandbox container."""

    backend: str = "local"  # local | docker | legacy_shell
    container: str = "mose-sandbox"
    image: str = "ubuntu:24.04"
    network: str = "none"
    read_only_rootfs: bool = True
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    timeout_default: int = 60
    workspace_mount: str = "/workspace"


@dataclass
class LearningConfig:
    """Skill proposal/learning loop and periodic skill-quality review.

    The learning loop NEVER auto-builds or auto-modifies a skill. A proposal is
    written to ``pending_dir`` and a human must approve via the registered
    callback (Signal by default) before the full skill body is generated.
    """

    enabled: bool = True
    pending_dir: str = "skills/pending"
    rejected_dir: str = "skills/rejected"
    # Kept for backward compatibility. Ignored: approval is ALWAYS required.
    approval_required: bool = True
    min_tools_used: int = 3
    skill_loading_mode: str = "full"  # full | level_0
    # Review job: a scheduled pass that reports on skill failure rates.
    skill_review_failure_threshold: float = 0.3
    review_interval_hours: int = 168  # weekly
    review_min_samples: int = 5
    review_log_dir: str = "data/logs"
    review_startup_delay_seconds: int = 300
    # Grace window given to the admin on startup when an approved-but-unbuilt
    # skill is detected (crashed mid-draft). The build auto-proceeds after
    # this delay unless the admin replies "stop <slug>" / "cancel <slug>".
    build_grace_window_seconds: int = 900


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    observe: ObserveConfig = field(default_factory=ObserveConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    terminal: TerminalConfig = field(default_factory=TerminalConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    root_dir: Path = _ROOT


def _apply_section(target, data: dict) -> None:
    for key, value in data.items():
        if hasattr(target, key):
            expected = type(getattr(target, key))
            setattr(target, key, expected(value))


def load_config(config_path: Path | None = None) -> Config:
    """Load config from TOML file, then override with env vars."""
    if config_path is None:
        config_path = _ROOT / "config.toml"

    cfg = Config()

    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        if "llm" in raw:
            _apply_section(cfg.llm, raw["llm"])
        if "discord" in raw:
            _apply_section(cfg.discord, raw["discord"])
        if "signal" in raw:
            _apply_section(cfg.signal, raw["signal"])
        if "memory" in raw:
            _apply_section(cfg.memory, raw["memory"])
        if "observe" in raw:
            _apply_section(cfg.observe, raw["observe"])
        if "agent" in raw:
            _apply_section(cfg.agent, raw["agent"])
        if "terminal" in raw:
            _apply_section(cfg.terminal, raw["terminal"])
        if "learning" in raw:
            _apply_section(cfg.learning, raw["learning"])

    # Env var overrides
    if token := os.environ.get("DISCORD_TOKEN"):
        cfg.discord.token = token
    if phone := os.environ.get("SIGNAL_PHONE"):
        cfg.signal.phone_number = phone
    if admin := os.environ.get("SIGNAL_ADMIN"):
        cfg.signal.admin_recipient = admin
    if endpoint := os.environ.get("LLM_ENDPOINT"):
        cfg.llm.endpoint = endpoint
    if model := os.environ.get("LLM_MODEL"):
        cfg.llm.model = model
    if (ctx := os.environ.get("LLM_CONTEXT_WINDOW")) is not None and str(ctx).strip() != "":
        cfg.llm.context_window = int(ctx)
    if (mt := os.environ.get("LLM_MAX_TOKENS")) is not None and str(mt).strip() != "":
        cfg.llm.max_tokens = int(mt)
    if (temp := os.environ.get("LLM_TEMPERATURE")) is not None and str(temp).strip() != "":
        cfg.llm.temperature = float(temp)
    if db_path := os.environ.get("MEMORY_DB_PATH"):
        cfg.memory.db_path = db_path
    if log_dir := os.environ.get("LOG_DIR"):
        cfg.observe.log_dir = log_dir
    if api_key := os.environ.get("LLM_API_KEY"):
        cfg.llm.api_key = api_key
    if provider := os.environ.get("LLM_PROVIDER"):
        cfg.llm.provider = provider

    # Resolve relative paths against project root
    if not Path(cfg.memory.db_path).is_absolute():
        cfg.memory.db_path = str(cfg.root_dir / cfg.memory.db_path)
    if not Path(cfg.observe.log_dir).is_absolute():
        cfg.observe.log_dir = str(cfg.root_dir / cfg.observe.log_dir)
    if not Path(cfg.agent.workspace).is_absolute():
        cfg.agent.workspace = str(cfg.root_dir / cfg.agent.workspace)
    if not Path(cfg.agent.skills_path).is_absolute():
        cfg.agent.skills_path = str(cfg.root_dir / cfg.agent.skills_path)
    if not Path(cfg.learning.pending_dir).is_absolute():
        cfg.learning.pending_dir = str(cfg.root_dir / cfg.learning.pending_dir)
    if not Path(cfg.learning.rejected_dir).is_absolute():
        cfg.learning.rejected_dir = str(cfg.root_dir / cfg.learning.rejected_dir)
    if not Path(cfg.learning.review_log_dir).is_absolute():
        cfg.learning.review_log_dir = str(cfg.root_dir / cfg.learning.review_log_dir)

    return cfg
