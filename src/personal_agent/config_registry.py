"""Lightweight registry for known runtime configuration fields."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConfigField:
    path: str
    attr: str
    source: str
    default: Any
    value_type: str
    section: str
    description: str
    sensitive: bool = False

    def as_dict(self, *, value: Any = None, include_value: bool = False) -> dict[str, Any]:
        data = {
            "path": self.path,
            "attr": self.attr,
            "source": self.source,
            "section": self.section,
            "default": _json_safe(self.default),
            "value_type": self.value_type,
            "sensitive": self.sensitive,
            "description": self.description,
        }
        if include_value:
            data.update(_value_payload(value, sensitive=self.sensitive))
        return data


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("execution.mode", "execution_mode", "config.yaml", "standard", "str", "execution", "Execution mode profile."),
    ConfigField("execution.policy", "execution_policy_overrides", "config.yaml", {}, "dict", "execution", "Execution policy overrides."),
    ConfigField("LLM_PROVIDER", "llm_provider", ".env", "deepseek", "str", "llm", "LLM provider."),
    ConfigField("LLM_API_KEY", "llm_api_key", ".env", "", "str", "llm", "LLM API key.", sensitive=True),
    ConfigField("LLM_BASE_URL", "llm_base_url", ".env", "", "str", "llm", "LLM base URL."),
    ConfigField("LLM_MODEL", "llm_model", ".env", "deepseek-chat", "str", "llm", "LLM model name."),
    ConfigField("LLM_API_MODE", "llm_api_mode", ".env", "auto", "str", "llm", "LLM API compatibility mode."),
    ConfigField("LLM_MAX_TOKENS", "llm_max_tokens", ".env", 4096, "int", "llm", "Maximum LLM output tokens."),
    ConfigField("FEISHU_APP_ID", "feishu_app_id", ".env", "", "str", "platforms", "Feishu app id."),
    ConfigField("FEISHU_APP_SECRET", "feishu_app_secret", ".env", "", "str", "platforms", "Feishu app secret.", sensitive=True),
    ConfigField("TELEGRAM_BOT_TOKEN", "telegram_bot_token", ".env", "", "str", "platforms", "Telegram bot token.", sensitive=True),
    ConfigField("WEIXIN_TOKEN", "weixin_token", ".env", "", "str", "platforms", "WeChat token.", sensitive=True),
    ConfigField("WEIXIN_ACCOUNT_ID", "weixin_account_id", ".env", "", "str", "platforms", "WeChat account id."),
    ConfigField("WEIXIN_USER_ID", "weixin_user_id", ".env", "", "str", "platforms", "WeChat user id."),
    ConfigField("WEIXIN_BASE_URL", "weixin_base_url", ".env", "https://ilinkai.weixin.qq.com", "str", "platforms", "WeChat API base URL."),
    ConfigField("QQ_BOT_BASE_URL", "qq_bot_base_url", ".env", "", "str", "platforms", "QQ/OneBot HTTP base URL."),
    ConfigField("QQ_BOT_TOKEN", "qq_bot_token", ".env", "", "str", "platforms", "QQ/OneBot token.", sensitive=True),
    ConfigField("QQ_BOT_WEBHOOK_SECRET", "qq_bot_webhook_secret", ".env", "", "str", "platforms", "QQ webhook secret.", sensitive=True),
    ConfigField("agent.max_iterations", "max_iterations", "config.yaml", 30, "int", "agent", "Maximum agent loop iterations."),
    ConfigField("agent.max_tool_calls_per_turn", "max_tool_calls_per_turn", "config.yaml", 20, "int", "agent", "Maximum tool calls per turn."),
    ConfigField("agents.max_concurrent_runs", "agent_runtime_max_concurrent_runs", "config.yaml", 4, "int", "agents", "Maximum concurrent delegated agent runs."),
    ConfigField("agents.max_tool_calls", "agent_runtime_max_tool_calls", "config.yaml", 10, "int", "agents", "Maximum delegated agent tool calls."),
    ConfigField("agents.max_tokens", "agent_runtime_max_tokens", "config.yaml", 4096, "int", "agents", "Delegated agent token budget."),
    ConfigField("agents.history_limit", "agent_runtime_history_limit", "config.yaml", 100, "int", "agents", "Delegated agent history limit."),
    ConfigField("storage.data_dir", "agent_data_dir", "config.yaml", "./data", "path", "storage", "Runtime data directory."),
    ConfigField("storage.log_level", "log_level", "config.yaml", "INFO", "str", "storage", "Log level."),
    ConfigField("toolsets.enabled", "enabled_toolsets", "config.yaml", ["all"], "list", "toolsets", "Enabled toolsets."),
    ConfigField("compression.engine", "compressor_engine", "config.yaml", "compressor", "str", "compression", "Compression engine."),
    ConfigField("compression.model", "compressor_model", "config.yaml", "", "str", "compression", "Compression model."),
    ConfigField("compression.max_tokens", "compressor_max_tokens", "config.yaml", 500, "int", "compression", "Compression max tokens."),
    ConfigField("compression.tail_token_budget", "tail_token_budget", "config.yaml", 20000, "int", "compression", "Tail token budget."),
    ConfigField("compression.threshold_ratio", "compression_threshold_ratio", "config.yaml", 0.6, "float", "compression", "Compression threshold ratio."),
    ConfigField("memory.provider", "memory_provider", "config.yaml", "file", "str", "memory", "Built-in memory provider."),
    ConfigField("memory.external_provider", "memory_external_provider", "config.yaml", "none", "str", "memory", "External memory provider."),
    ConfigField("memory.review_interval", "memory_review_interval", "config.yaml", 10, "int", "memory", "Memory review interval."),
    ConfigField("memory.embedding.model", "memory_embedding_model", "config.yaml", "BAAI/bge-small-zh-v1.5", "str", "memory", "Embedding model."),
    ConfigField("memory.embedding.relevance_threshold", "memory_embedding_relevance_threshold", "config.yaml", 0.3, "float", "memory", "Embedding relevance threshold."),
    ConfigField("memory.embedding.max_prefetch", "memory_embedding_max_prefetch", "config.yaml", 3, "int", "memory", "Embedding prefetch limit."),
    ConfigField("memory.embedding.chunk_size", "memory_embedding_chunk_size", "config.yaml", 800, "int", "memory", "Embedding chunk size."),
    ConfigField("cron.enabled", "enable_cron", "config.yaml", False, "bool", "cron", "Enable cron scheduler."),
    ConfigField("sandbox.roots", "sandbox_roots", "config.yaml", ["./data"], "list[path]", "sandbox", "Sandbox root directories."),
    ConfigField("sandbox.blocked", "sandbox_blocked", "config.yaml", [], "list", "sandbox", "Blocked sandbox path patterns."),
    ConfigField("sandbox.bash_work_dir", "bash_work_dir", "config.yaml", "./data", "path", "sandbox", "Bash working directory."),
    ConfigField("sandbox.bash_restrict_paths", "bash_restrict_paths", "config.yaml", True, "bool", "sandbox", "Restrict bash paths."),
    ConfigField("sandbox.bash_allow_network", "bash_allow_network", "config.yaml", False, "bool", "sandbox", "Allow bash network commands."),
    ConfigField("sandbox.file_max_write_bytes", "file_max_write_bytes", "config.yaml", 100000, "int", "sandbox", "Maximum file write size."),
    ConfigField("sandbox.audit_enabled", "audit_enabled", "config.yaml", True, "bool", "sandbox", "Enable tool audit logging."),
    ConfigField("gateway.platform_reconnect_delays", "platform_reconnect_delays", "config.yaml", [1, 2, 5, 10, 30, 60], "list[int]", "gateway", "Platform reconnect delays."),
    ConfigField("gateway.platform_pending_warning_threshold", "platform_pending_warning_threshold", "config.yaml", 10, "int", "gateway", "Pending message warning threshold."),
    ConfigField("gateway.platform_chat_locks_maxsize", "platform_chat_locks_maxsize", "config.yaml", 64, "int", "gateway", "Gateway chat lock cache size."),
    ConfigField("gateway.platform_message_dedupe_max_size", "platform_message_dedupe_max_size", "config.yaml", 1024, "int", "gateway", "Gateway message dedupe cache size."),
    ConfigField("gateway.platform_send_max_retries", "platform_send_max_retries", "config.yaml", 2, "int", "gateway", "Platform send retry limit."),
    ConfigField("session.expire_days", "session_expire_days", "config.yaml", 30, "int", "session", "Session expiry in days."),
    ConfigField("session.override", "session_override", "config.yaml", {}, "dict", "session", "Session routing overrides."),
    ConfigField("mcp.enabled", "mcp_enabled", "config.yaml", False, "bool", "mcp", "Enable MCP."),
    ConfigField("mcp.servers", "mcp_servers", "config.yaml", [], "list", "mcp", "Configured MCP servers."),
    ConfigField("plugins.dirs", "plugins_dirs", "config.yaml", ["./plugins", "./data/plugins"], "list[path]", "plugins", "Plugin directories."),
    ConfigField("plugins.enabled", "plugins_enabled", "config.yaml", [], "list", "plugins", "Enabled plugins."),
    ConfigField("plugins.disabled", "plugins_disabled", "config.yaml", [], "list", "plugins", "Disabled plugins."),
    ConfigField("auth.enabled", "auth_enabled", "config.yaml", False, "bool", "auth", "Enable auth."),
    ConfigField("auth.admins", "auth_admins", "config.yaml", [], "list", "auth", "Admin users."),
    ConfigField("auth.allowed_users", "auth_allowed_users", "config.yaml", [], "list", "auth", "Allowed users."),
    ConfigField("profiles", "profile_map", "config.yaml/.env", {}, "dict", "profiles", "Session profile map."),
)


def config_fields_by_section() -> dict[str, list[ConfigField]]:
    result: dict[str, list[ConfigField]] = {}
    for field in CONFIG_FIELDS:
        result.setdefault(field.section, []).append(field)
    return {section: sorted(fields, key=lambda item: item.path) for section, fields in sorted(result.items())}


def registry_fields_summary() -> dict[str, Any]:
    sections = config_fields_by_section()
    return {
        "field_count": len(CONFIG_FIELDS),
        "sections": {
            section: [field.as_dict() for field in fields]
            for section, fields in sections.items()
        },
    }


def effective_config_snapshot(settings) -> dict[str, Any]:
    fields = []
    sections: dict[str, list[dict[str, Any]]] = {}
    for field in CONFIG_FIELDS:
        value = getattr(settings, field.attr, None)
        item = field.as_dict(value=value, include_value=True)
        fields.append(item)
        sections.setdefault(field.section, []).append(item)
    return {
        "field_count": len(fields),
        "fields": fields,
        "sections": {
            section: sorted(items, key=lambda item: item["path"])
            for section, items in sorted(sections.items())
        },
    }


def _value_payload(value: Any, *, sensitive: bool) -> dict[str, Any]:
    if sensitive:
        is_set = bool(value)
        return {"value": "<set>" if is_set else "<unset>", "is_set": is_set}
    return {"value": _json_safe(value)}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    return value
