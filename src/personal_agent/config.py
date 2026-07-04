"""Central config — .env for LLM/secrets, config.yaml for behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _load_yaml(path: str = "config.yaml") -> dict[str, Any]:
    if not Path(path).exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_env(path: str = ".env") -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: v or "" for k, v in dotenv_values(path).items()}


def _load_int_list(value: Any, *, default: list[int], field_name: str) -> list[int]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of integers")
    result: list[int] = []
    for item in value:
        number = int(item)
        if number <= 0:
            raise ValueError(f"{field_name} must contain only positive integers")
        result.append(number)
    return result or list(default)


class Settings:
    def __init__(self, **overrides: Any) -> None:
        yaml_cfg = _load_yaml()
        env = _load_env()
        self.raw_env: dict[str, str] = env
        self.raw_config: dict[str, Any] = yaml_cfg

        # ── Execution mode (from config.yaml) ──
        execution = yaml_cfg.get("execution", {})
        if not isinstance(execution, dict):
            execution = {}
        self.execution_mode: str = str(execution.get("mode", "standard") or "standard").strip().lower()
        execution_policy = execution.get("policy", {})
        self.execution_policy_overrides: dict[str, Any] = (
            dict(execution_policy) if isinstance(execution_policy, dict) else {}
        )

        # ── LLM (from .env) ──
        self.llm_api_key: str = env.get("LLM_API_KEY", "")
        self.llm_base_url: str = env.get("LLM_BASE_URL", "")
        self.llm_model: str = env.get("LLM_MODEL", "deepseek-chat")
        self.llm_api_mode: str = env.get("LLM_API_MODE", "auto")
        self.llm_provider: str = env.get("LLM_PROVIDER", "deepseek")
        self.llm_max_tokens: int = int(env.get("LLM_MAX_TOKENS", "4096"))

        # ── Platforms (from .env) ──
        self.feishu_app_id: str = env.get("FEISHU_APP_ID", "")
        self.feishu_app_secret: str = env.get("FEISHU_APP_SECRET", "")
        self.telegram_bot_token: str = env.get("TELEGRAM_BOT_TOKEN", "")
        self.weixin_token: str = env.get("WEIXIN_TOKEN", "")
        self.weixin_account_id: str = env.get("WEIXIN_ACCOUNT_ID", "")
        self.weixin_user_id: str = env.get("WEIXIN_USER_ID", "")
        self.weixin_base_url: str = env.get("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com")
        self.qq_bot_base_url: str = env.get("QQ_BOT_BASE_URL", "")
        self.qq_bot_token: str = env.get("QQ_BOT_TOKEN", "")
        self.qq_bot_webhook_secret: str = env.get("QQ_BOT_WEBHOOK_SECRET", "")

        # ── Agent (from config.yaml) ──
        agent = yaml_cfg.get("agent", {})
        self.max_iterations: int = agent.get("max_iterations", 30)
        self.max_tool_calls_per_turn: int = agent.get("max_tool_calls_per_turn", 20)

        # ── Delegated agents (from config.yaml) ──
        agents = yaml_cfg.get("agents", {})
        self.agent_runtime_max_concurrent_runs: int = agents.get("max_concurrent_runs", 4)
        self.agent_runtime_max_tool_calls: int = agents.get("max_tool_calls", 10)
        self.agent_runtime_max_tokens: int = agents.get("max_tokens", 4096)
        self.agent_runtime_history_limit: int = agents.get("history_limit", 100)

        # ── Storage (from config.yaml) ──
        storage = yaml_cfg.get("storage", {})
        self.agent_data_dir: Path = Path(storage.get("data_dir", "./data"))
        self.log_level: str = storage.get("log_level", "INFO")

        # ── Toolsets (from config.yaml) ──
        toolsets = yaml_cfg.get("toolsets", {})
        self.enabled_toolsets: list[str] | None = toolsets.get("enabled", ["all"])

        # ── Compression (from config.yaml) ──
        comp = yaml_cfg.get("compression", {})
        self.compressor_engine: str = comp.get("engine", "compressor")
        self.compressor_model: str = comp.get("model", "")
        self.compressor_max_tokens: int = comp.get("max_tokens", 500)
        self.tail_token_budget: int = comp.get("tail_token_budget", 20000)
        self.compression_threshold_ratio: float = comp.get("threshold_ratio", 0.6)

        # ── Memory (from config.yaml) ──
        memory = yaml_cfg.get("memory", {})
        self.memory_provider: str = memory.get("provider", "file")
        self.memory_external_provider: str = memory.get("external_provider", "none")
        self.memory_review_interval: int = memory.get("review_interval", 10)
        embedding = memory.get("embedding", {})
        self.memory_embedding_model: str = embedding.get("model", "BAAI/bge-small-zh-v1.5")
        self.memory_embedding_relevance_threshold: float = embedding.get("relevance_threshold", 0.3)
        self.memory_embedding_max_prefetch: int = embedding.get("max_prefetch", 3)
        self.memory_embedding_chunk_size: int = embedding.get("chunk_size", 800)

        # ── Cron (from config.yaml) ──
        cron = yaml_cfg.get("cron", {})
        self.enable_cron: bool = cron.get("enabled", False)
        self.cron_jobs_path: Path = Path("data/cron")

        # ── Sandbox (from config.yaml) ──
        sandbox = yaml_cfg.get("sandbox", {})

        # roots: list or comma-separated string
        raw_roots = sandbox.get("roots", ["./data"])
        if isinstance(raw_roots, str):
            self.sandbox_roots: list[Path] = [Path(p.strip()) for p in raw_roots.split(",") if p.strip()]
        elif isinstance(raw_roots, list):
            self.sandbox_roots: list[Path] = [Path(p) for p in raw_roots]
        else:
            self.sandbox_roots: list[Path] = [Path("./data")]

        # blocked: list of glob patterns
        self.sandbox_blocked: list[str] = sandbox.get("blocked", [])

        self.bash_work_dir: Path = Path(sandbox.get("bash_work_dir", "./data"))
        self.bash_restrict_paths: bool = sandbox.get("bash_restrict_paths", True)
        self.bash_allow_network: bool = sandbox.get("bash_allow_network", False)
        self.file_max_write_bytes: int = sandbox.get("file_max_write_bytes", 100000)
        self.audit_enabled: bool = sandbox.get("audit_enabled", True)

        # ── Gateway (from config.yaml) ──
        gateway = yaml_cfg.get("gateway", {})
        self.platform_reconnect_delays: list[int] = _load_int_list(
            gateway.get("platform_reconnect_delays", [1, 2, 5, 10, 30, 60]),
            default=[1, 2, 5, 10, 30, 60],
            field_name="gateway.platform_reconnect_delays",
        )
        self.platform_pending_warning_threshold: int = gateway.get(
            "platform_pending_warning_threshold", 10
        )
        self.platform_chat_locks_maxsize: int = gateway.get("platform_chat_locks_maxsize", 64)
        self.platform_message_dedupe_max_size: int = gateway.get(
            "platform_message_dedupe_max_size", 1024
        )
        self.platform_send_max_retries: int = gateway.get("platform_send_max_retries", 2)

        # ── Session (from config.yaml) ──
        session = yaml_cfg.get("session", {})
        self.session_expire_days: int = session.get("expire_days", 30)
        self.session_override: dict[str, str] = session.get("override", {})

        # ── MCP (from config.yaml) ──
        mcp = yaml_cfg.get("mcp", {})
        self.mcp_enabled: bool = mcp.get("enabled", False)
        self.mcp_servers: list[dict] = mcp.get("servers", [])

        # ── Plugins (from config.yaml) ──
        plugins = yaml_cfg.get("plugins", {})
        raw_plugin_dirs = plugins.get("dirs", ["./plugins", "./data/plugins"])
        if isinstance(raw_plugin_dirs, str):
            self.plugins_dirs: list[Path] = [
                Path(p.strip()) for p in raw_plugin_dirs.split(",") if p.strip()
            ]
        else:
            self.plugins_dirs = [Path(p) for p in raw_plugin_dirs]
        self.plugins_enabled: list[str] = plugins.get("enabled", [])
        self.plugins_disabled: list[str] = plugins.get("disabled", [])

        # ── Auth (from config.yaml) ──
        auth = yaml_cfg.get("auth", {})
        self.auth_enabled: bool = auth.get("enabled", False)
        self.auth_admins: list[str] = auth.get("admins", [])
        self.auth_allowed_users: list[str] = auth.get("allowed_users", [])

        # ── Profiles (from config.yaml + .env override) ──
        profiles = yaml_cfg.get("profiles") or {}
        self.profile_map: dict[str, str] = dict(profiles)
        # .env override: PROFILES={"wechat:xxx:xxx":"girlfriend"}
        profiles_env = env.get("PROFILES", "")
        if profiles_env:
            try:
                import json
                self.profile_map.update(json.loads(profiles_env))
            except json.JSONDecodeError:
                pass

        for key, value in overrides.items():
            if key.endswith("_dir") and isinstance(value, str):
                value = Path(value)
            setattr(self, key, value)

        from personal_agent.execution import resolve_execution_policy
        self.execution_policy = resolve_execution_policy(self)
