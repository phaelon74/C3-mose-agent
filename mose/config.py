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


def _env_optional_bool(name: str) -> bool | None:
    """Parse optional env as bool; None if unset or blank; None if unrecognized."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.lower() in ("1", "true", "yes", "on"):
        return True
    if s.lower() in ("0", "false", "no", "off"):
        return False
    return None


@dataclass
class LLMConfig:
    endpoint: str = "http://localhost:8001/v1"
    model: str = "worker-agent"
    max_tokens: int = 16384
    temperature: float = 1.0
    context_window: int = 98304
    # When True, chat requests omit the temperature field entirely (server default).
    omit_temperature: bool = False
    # TabbyAPI and many OpenAI-compatible servers require Bearer auth; empty = no key (local vLLM).
    api_key: str = ""
    provider: str = "openai_compat"  # openai_compat | tabby | vllm | bedrock
    vision_enabled: bool = True
    vision_tokens_per_image: int = 1536


@dataclass
class ContextCompressConfig:
    """Chunk-and-summarize when payloads would exceed the model context bound."""

    enabled: bool = True
    safety_margin_tokens: int = 4096
    chunk_input_tokens: int = 12000
    max_recursion_depth: int = 4
    min_compress_tokens: int = 0
    # Chars threshold before process_large_output considers compression (0 = derive from context).
    large_output_threshold: int = 0


@dataclass
class DiscordConfig:
    token: str = ""


@dataclass
class SignalConfig:
    # Linked device account (signal-cli -a +...). Not a message destination.
    phone_number: str = ""
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 7583
    # Base64 group ids from signal-cli listGroups / JSON-RPC listGroups.
    engagement_group_id: str = ""
    admin_group_id: str = ""
    # Seconds to wait for a human response on a skill proposal (12 hours).
    proposal_timeout_seconds: int = 43200
    max_attachment_bytes: int = 10_485_760
    max_images_per_message: int = 4
    allowed_text_suffixes: list[str] = field(
        default_factory=lambda: [".txt", ".log", ".json", ".md"],
    )
    allowed_image_mime_types: list[str] = field(
        default_factory=lambda: [
            "image/jpeg",
            "image/jpg",
            "image/png",
            "image/webp",
            "image/gif",
        ],
    )


def signal_runtime_ready(signal: SignalConfig) -> bool:
    """True when the Signal bot should run: linked account plus both group ids."""
    return bool(
        (signal.phone_number or "").strip()
        and (signal.engagement_group_id or "").strip()
        and (signal.admin_group_id or "").strip()
    )


def assert_signal_account_requires_groups(signal: SignalConfig) -> None:
    """Exit non-zero if SIGNAL_PHONE is set but group ids are incomplete."""
    import sys

    phone = (signal.phone_number or "").strip()
    eng = (signal.engagement_group_id or "").strip()
    adm = (signal.admin_group_id or "").strip()
    if phone and (not eng or not adm):
        print(
            "Signal is misconfigured: SIGNAL_PHONE is set but SIGNAL_ENGAGEMENT_GROUP_ID "
            "and/or SIGNAL_ADMIN_GROUP_ID is missing.\n"
            "See INSTALL.md section E (Signal bot setup).",
            file=sys.stderr,
        )
        raise SystemExit(1)


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
    # Legacy: ignored by the agent. MCP tool schemas from connected servers are always merged.
    inline_mcp_tools: bool = True
    # If non-empty, only tools whose name starts with "<server>__" are merged.
    inline_mcp_servers: list[str] = field(default_factory=list)
    # Legacy soft cap (no longer enforced).
    inline_mcp_tools_soft_cap: int = 200


@dataclass
class TerminalConfig:
    """Where shell tools run: local bash argv, or docker exec into a sandbox container.

    Sandbox image, capabilities, and networks are defined in docker-compose.yml, not here.
    """

    backend: str = "local"  # local | docker | legacy_shell
    container: str = "mose-sandbox"
    workspace_mount: str = "/workspace"


@dataclass
class PortalConfig:
    """Code Mode portal integration: HTTP approval bridge for mutating MCP calls from the sandbox."""

    # When True, ``python -m mose`` starts POST /approve on approval_bridge_port (see mose/approval_bridge.py).
    enabled: bool = False
    approval_bridge_host: str = "0.0.0.0"
    approval_bridge_port: int = 9100
    # Reserved for portal_codemode_execute defaults (Phase 4 wiring); documented here for operators.
    code_timeout_seconds: int = 30
    code_max_timeout: int = 120


@dataclass
class TrackersConfig:
    """Scheduled data collection (trackers) and retention."""

    enabled: bool = True
    sample_retention_days: int = 90
    rollup_retention_days: int = 730
    reconcile_interval_seconds: int = 60
    default_schedule_seconds: int = 5
    snapshot_interval_seconds: int = 300
    failure_threshold: int = 30
    default_recipient: str = "signal:admin"
    compaction_interval_hours: int = 6
    compaction_startup_delay_seconds: int = 120
    active_trackers_prompt_chars: int = 500
    active_trackers_max_lines: int = 12
    code_timeout_seconds: int = 60


@dataclass
class SchedulerConfig:
    """Calendar-aware scheduled agent tasks."""

    enabled: bool = True
    timezone: str = "America/Chicago"
    reconcile_interval_seconds: int = 60
    run_timeout_seconds: int = 600
    max_concurrent_runs: int = 1
    failure_threshold: int = 5
    default_recipient: str = "signal:admin"
    run_retention_days: int = 90


@dataclass
class UpcomingConfig:
    """Upcoming movies/TV/anime catalog (TMDB) and weekly *arr recommendations."""

    enabled: bool = True
    db_path: str = "data/upcoming.db"
    tmdb_api_key: str = ""
    daily_hour: int = 5
    weekly_weekday: str = "monday"
    weekly_hour: int = 9
    timezone: str = ""  # empty → inherit [scheduler].timezone
    sync_max_age_hours: int = 26
    recommendation_horizon_days: int = 180
    min_popularity: float = 5.0
    min_vote_count: int = 20
    cap_movies: int = 40
    cap_tv: int = 40
    cap_anime: int = 20
    failure_threshold: int = 3
    startup_delay_seconds: int = 180
    reconcile_interval_seconds: int = 60
    catalog_retention_years: int = 2
    recommendation_retention_days: int = 90
    sonarr_tv_root_match: str = "/TV"
    sonarr_anime_root_match: str = "/Anime"
    sonarr_tv_root_path: str = ""
    sonarr_anime_root_path: str = ""
    radarr_root_path: str = ""
    radarr_quality_profile_id: int = 0
    sonarr_tv_quality_profile_id: int = 0
    sonarr_anime_quality_profile_id: int = 0
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    tvdb_api_key: str = ""


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
    context_compress: ContextCompressConfig = field(default_factory=ContextCompressConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    observe: ObserveConfig = field(default_factory=ObserveConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    terminal: TerminalConfig = field(default_factory=TerminalConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    trackers: TrackersConfig = field(default_factory=TrackersConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    upcoming: UpcomingConfig = field(default_factory=UpcomingConfig)
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
        if "context_compress" in raw:
            _apply_section(cfg.context_compress, raw["context_compress"])
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
        if "trackers" in raw:
            _apply_section(cfg.trackers, raw["trackers"])
        if "scheduler" in raw:
            _apply_section(cfg.scheduler, raw["scheduler"])
        if "portal" in raw:
            _apply_section(cfg.portal, raw["portal"])
        if "upcoming" in raw:
            _apply_section(cfg.upcoming, raw["upcoming"])

    # Env var overrides
    if token := os.environ.get("DISCORD_TOKEN"):
        cfg.discord.token = token
    if phone := os.environ.get("SIGNAL_PHONE"):
        cfg.signal.phone_number = phone.strip()
    if gid := os.environ.get("SIGNAL_ENGAGEMENT_GROUP_ID"):
        cfg.signal.engagement_group_id = gid.strip()
    if gid := os.environ.get("SIGNAL_ADMIN_GROUP_ID"):
        cfg.signal.admin_group_id = gid.strip()
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
    if (ve := _env_optional_bool("LLM_VISION_ENABLED")) is not None:
        cfg.llm.vision_enabled = ve
    if (vt := os.environ.get("LLM_VISION_TOKENS_PER_IMAGE")) is not None and str(vt).strip():
        cfg.llm.vision_tokens_per_image = int(vt)
    if (smb := os.environ.get("SIGNAL_MAX_ATTACHMENT_BYTES")) is not None and str(smb).strip():
        cfg.signal.max_attachment_bytes = int(smb)
    if (sim := os.environ.get("SIGNAL_MAX_IMAGES_PER_MESSAGE")) is not None and str(sim).strip():
        cfg.signal.max_images_per_message = int(sim)
    if (omit_temp := _env_optional_bool("LLM_OMIT_TEMPERATURE")) is not None:
        cfg.llm.omit_temperature = omit_temp

    if (cc := _env_optional_bool("CONTEXT_COMPRESS_ENABLED")) is not None:
        cfg.context_compress.enabled = cc
    if (sm := os.environ.get("CONTEXT_COMPRESS_SAFETY_MARGIN")) is not None and str(sm).strip():
        cfg.context_compress.safety_margin_tokens = int(sm)
    if (ci := os.environ.get("CONTEXT_COMPRESS_CHUNK_TOKENS")) is not None and str(ci).strip():
        cfg.context_compress.chunk_input_tokens = int(ci)
    if (md := os.environ.get("CONTEXT_COMPRESS_MAX_DEPTH")) is not None and str(md).strip():
        cfg.context_compress.max_recursion_depth = int(md)

    if (pe := _env_optional_bool("MOSE_PORTAL_ENABLED")) is not None:
        cfg.portal.enabled = pe
    if (ph := os.environ.get("MOSE_PORTAL_APPROVAL_HOST")) is not None and str(ph).strip():
        cfg.portal.approval_bridge_host = str(ph).strip()
    if (pp := os.environ.get("MOSE_PORTAL_APPROVAL_PORT")) is not None and str(pp).strip():
        try:
            cfg.portal.approval_bridge_port = int(str(pp).strip())
        except ValueError:
            pass

    if (trd := os.environ.get("TRACKERS_SAMPLE_RETENTION_DAYS")) is not None and str(trd).strip():
        cfg.trackers.sample_retention_days = int(trd)
    if (tds := os.environ.get("TRACKERS_DEFAULT_SCHEDULE_SECONDS")) is not None and str(tds).strip():
        cfg.trackers.default_schedule_seconds = int(tds)
    if (tsi := os.environ.get("TRACKERS_SNAPSHOT_INTERVAL_SECONDS")) is not None and str(tsi).strip():
        cfg.trackers.snapshot_interval_seconds = int(tsi)
    if (tft := os.environ.get("TRACKERS_FAILURE_THRESHOLD")) is not None and str(tft).strip():
        cfg.trackers.failure_threshold = int(tft)
    if (tci := os.environ.get("TRACKERS_COMPACTION_INTERVAL_HOURS")) is not None and str(tci).strip():
        cfg.trackers.compaction_interval_hours = int(tci)

    if (se := _env_optional_bool("SCHEDULER_ENABLED")) is not None:
        cfg.scheduler.enabled = se
    if (stz := os.environ.get("SCHEDULER_TIMEZONE")) is not None and str(stz).strip():
        cfg.scheduler.timezone = str(stz).strip()

    if (ue := _env_optional_bool("UPCOMING_ENABLED")) is not None:
        cfg.upcoming.enabled = ue
    if tmdb := os.environ.get("TMDB_API_KEY"):
        cfg.upcoming.tmdb_api_key = tmdb.strip()
    if ru := os.environ.get("RADARR_URL"):
        cfg.upcoming.radarr_url = ru.strip()
    if rk := os.environ.get("RADARR_API_KEY"):
        cfg.upcoming.radarr_api_key = rk.strip()
    if su := os.environ.get("SONARR_URL"):
        cfg.upcoming.sonarr_url = su.strip()
    if sk := os.environ.get("SONARR_API_KEY"):
        cfg.upcoming.sonarr_api_key = sk.strip()
    if tvdb := os.environ.get("TVDB_API_KEY"):
        cfg.upcoming.tvdb_api_key = tvdb.strip()

    cfg.signal.phone_number = (cfg.signal.phone_number or "").strip()
    cfg.signal.engagement_group_id = (cfg.signal.engagement_group_id or "").strip()
    cfg.signal.admin_group_id = (cfg.signal.admin_group_id or "").strip()

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
    if not Path(cfg.upcoming.db_path).is_absolute():
        cfg.upcoming.db_path = str(cfg.root_dir / cfg.upcoming.db_path)
    if not (cfg.upcoming.timezone or "").strip():
        cfg.upcoming.timezone = cfg.scheduler.timezone

    return cfg
