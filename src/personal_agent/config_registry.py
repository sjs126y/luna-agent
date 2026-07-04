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
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[Any, ...] = ()
    allow_csv: bool = False
    required: bool = False
    template_default: Any = None
    owner: str = "core"
    namespace: str = ""
    plugin_key: str = ""
    env_key: str = ""
    yaml_path: str = ""
    runtime_type: str = ""

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
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices),
            "allow_csv": self.allow_csv,
            "required": self.required,
            "template_default": _json_safe(self.template_default),
            "owner": self.owner,
            "namespace": self.namespace,
            "plugin_key": self.plugin_key,
            "env_key": self.env_key,
            "yaml_path": self.yaml_path,
            "runtime_type": self.runtime_type,
        }
        if include_value:
            data.update(_value_payload(value, sensitive=self.sensitive))
        return data


@dataclass(frozen=True)
class ConfigSnapshot:
    fields: tuple[dict[str, Any], ...]
    values: dict[str, Any]
    attr_values: dict[str, Any]
    sources: dict[str, str]
    source_counts: dict[str, int]
    sections: dict[str, tuple[dict[str, Any], ...]]
    raw_env: dict[str, str]
    raw_config: dict[str, Any]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def field_count(self) -> int:
        return len(self.fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_count": self.field_count,
            "fields": [_json_safe(field) for field in self.fields],
            "values": _json_safe(self.values),
            "attr_values": _json_safe(_masked_attr_values(self.fields, self.attr_values)),
            "sources": dict(self.sources),
            "source_counts": dict(self.source_counts),
            "sections": {
                section: [_json_safe(item) for item in fields]
                for section, fields in self.sections.items()
            },
            "raw_env": _json_safe(_masked_raw_env(self.fields, self.raw_env)),
            "raw_config": _json_safe(self.raw_config),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


class ConfigRegistry:
    def __init__(self, fields: tuple[ConfigField, ...] | None = None) -> None:
        self._fields_by_path: dict[str, ConfigField] = {}
        self._fields_by_attr: dict[str, ConfigField] = {}
        if fields:
            for field in fields:
                self.register(field)

    @property
    def fields(self) -> tuple[ConfigField, ...]:
        return tuple(self._fields_by_path.values())

    def register(self, field: ConfigField) -> None:
        if field.path in self._fields_by_path:
            raise ValueError(f"Duplicate config field path: {field.path}")
        if field.attr in self._fields_by_attr:
            raise ValueError(f"Duplicate config field attr: {field.attr}")
        self._fields_by_path[field.path] = field
        self._fields_by_attr[field.attr] = field

    def get(self, path: str) -> ConfigField | None:
        return self._fields_by_path.get(path)

    def get_by_attr(self, attr: str) -> ConfigField | None:
        return self._fields_by_attr.get(attr)

    def sections(self) -> set[str]:
        return {field.section for field in self.fields}

    def by_section(self) -> dict[str, list[ConfigField]]:
        result: dict[str, list[ConfigField]] = {}
        for field in self.fields:
            result.setdefault(field.section, []).append(field)
        return {
            section: sorted(fields, key=lambda item: item.path)
            for section, fields in sorted(result.items())
        }

    def yaml_fields(self) -> tuple[ConfigField, ...]:
        return tuple(field for field in self.fields if "config.yaml" in field.source)

    def env_fields(self) -> tuple[ConfigField, ...]:
        return tuple(field for field in self.fields if ".env" in field.source)

    def yaml_known_sections(self) -> set[str]:
        return {_path_section(config_field_yaml_path(field)) for field in self.yaml_fields()}

    def yaml_known_keys_by_section(self) -> dict[str, set[str] | None]:
        result: dict[str, set[str] | None] = {}
        for field in self.yaml_fields():
            parts = config_field_yaml_path(field).split(".")
            section = parts[0]
            if len(parts) == 1:
                result[section] = None
                continue
            if section in result and result[section] is None:
                continue
            result.setdefault(section, set()).add(parts[1])
        return result

    def summary(self) -> dict[str, Any]:
        sections = self.by_section()
        return {
            "field_count": len(self.fields),
            "config_yaml_field_count": len(self.yaml_fields()),
            "env_field_count": len(self.env_fields()),
            "sections": {
                section: [field.as_dict() for field in fields]
                for section, fields in sections.items()
            },
        }

    def schema(self) -> dict[str, Any]:
        return {
            "version": 1,
            "field_count": len(self.fields),
            "fields": [field.as_dict() for field in sorted(self.fields, key=lambda item: item.path)],
            "sections": {
                section: [field.path for field in fields]
                for section, fields in self.by_section().items()
            },
        }

    def coverage(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        known_sections = sorted(self.yaml_known_sections())
        coverage = {
            "field_count": len(self.fields),
            "config_yaml_field_count": len(self.yaml_fields()),
            "env_field_count": len(self.env_fields()),
            "config_yaml_sections": known_sections,
            "config_yaml_section_count": len(known_sections),
        }
        if config is not None:
            known = self.yaml_known_sections()
            present = sorted(
                section for section in config
                if section in known and isinstance(config.get(section), dict)
            )
            coverage["present_config_sections"] = present
            coverage["present_config_section_count"] = len(present)
        return coverage

    def validate_config(self, config: dict[str, Any]) -> dict[str, list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        for field in self.yaml_fields():
            found, value = _raw_config_value(config, field.path)
            if not found:
                continue
            result = self.validate_value(field, value)
            errors.extend(result["errors"])
            warnings.extend(result["warnings"])
        return {"errors": _dedupe(errors), "warnings": _dedupe(warnings)}

    def validate_value(self, field: ConfigField, value: Any) -> dict[str, list[str]]:
        return _validate_field_value(field, value)

    def snapshot_from_settings(self, settings: Any) -> ConfigSnapshot:
        fields: list[dict[str, Any]] = []
        values: dict[str, Any] = {}
        attr_values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        sections: dict[str, list[dict[str, Any]]] = {}
        for field in self.fields:
            value = getattr(settings, field.attr, None)
            item = field.as_dict(value=value, include_value=True)
            fields.append(item)
            values[field.path] = item.get("value")
            attr_values[field.attr] = value
            sources[field.path] = field.source
            sections.setdefault(field.section, []).append(item)
        sorted_sections = {
            section: tuple(sorted(items, key=lambda item: item["path"]))
            for section, items in sorted(sections.items())
        }
        return ConfigSnapshot(
            fields=tuple(sorted(fields, key=lambda item: item["path"])),
            values=values,
            attr_values=attr_values,
            sources=sources,
            source_counts=_count_sources(sources),
            sections=sorted_sections,
            raw_env=getattr(settings, "raw_env", {}),
            raw_config=getattr(settings, "raw_config", {}),
        )


EXECUTION_MODES = ("guarded", "standard", "trusted", "sovereign")
LLM_PROVIDERS = ("anthropic", "deepseek", "openai", "openrouter")
LLM_API_MODES = ("anthropic_messages", "auto", "chat_completions")
COMPRESSION_ENGINES = ("compressor", "disabled", "none", "off", "simple")
MEMORY_PROVIDERS = ("file",)
EXTERNAL_MEMORY_PROVIDERS = ("embedding", "none")


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("execution.mode", "execution_mode", "config.yaml", "standard", "str", "execution", "Execution mode profile.", choices=EXECUTION_MODES),
    ConfigField("execution.policy", "execution_policy_overrides", "config.yaml", {}, "dict", "execution", "Execution policy overrides."),
    ConfigField("LLM_PROVIDER", "llm_provider", ".env", "deepseek", "str", "llm", "LLM provider.", choices=LLM_PROVIDERS),
    ConfigField("LLM_API_KEY", "llm_api_key", ".env", "", "str", "llm", "LLM API key.", sensitive=True),
    ConfigField("LLM_BASE_URL", "llm_base_url", ".env", "", "str", "llm", "LLM base URL."),
    ConfigField("LLM_MODEL", "llm_model", ".env", "deepseek-chat", "str", "llm", "LLM model name."),
    ConfigField("LLM_API_MODE", "llm_api_mode", ".env", "auto", "str", "llm", "LLM API compatibility mode."),
    ConfigField("LLM_MAX_TOKENS", "llm_max_tokens", ".env", 4096, "int", "llm", "Maximum LLM output tokens.", minimum=1),
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
    ConfigField("agent.max_iterations", "max_iterations", "config.yaml", 30, "int", "agent", "Maximum agent loop iterations.", minimum=1),
    ConfigField("agent.max_tool_calls_per_turn", "max_tool_calls_per_turn", "config.yaml", 20, "int", "agent", "Maximum tool calls per turn.", minimum=1),
    ConfigField("agents.max_concurrent_runs", "agent_runtime_max_concurrent_runs", "config.yaml", 4, "int", "agents", "Maximum concurrent delegated agent runs.", minimum=1),
    ConfigField("agents.max_tool_calls", "agent_runtime_max_tool_calls", "config.yaml", 10, "int", "agents", "Maximum delegated agent tool calls.", minimum=1),
    ConfigField("agents.max_tokens", "agent_runtime_max_tokens", "config.yaml", 4096, "int", "agents", "Delegated agent token budget.", minimum=1),
    ConfigField("agents.history_limit", "agent_runtime_history_limit", "config.yaml", 100, "int", "agents", "Delegated agent history limit.", minimum=1),
    ConfigField("storage.data_dir", "agent_data_dir", "config.yaml", "./data", "path", "storage", "Runtime data directory."),
    ConfigField("storage.log_level", "log_level", "config.yaml", "INFO", "str", "storage", "Log level."),
    ConfigField("toolsets.enabled", "enabled_toolsets", "config.yaml", ["all"], "list", "toolsets", "Enabled toolsets."),
    ConfigField("compression.engine", "compressor_engine", "config.yaml", "compressor", "str", "compression", "Compression engine.", choices=COMPRESSION_ENGINES),
    ConfigField("compression.model", "compressor_model", "config.yaml", "", "str", "compression", "Compression model."),
    ConfigField("compression.max_tokens", "compressor_max_tokens", "config.yaml", 500, "int", "compression", "Compression max tokens.", minimum=1),
    ConfigField("compression.tail_token_budget", "tail_token_budget", "config.yaml", 20000, "int", "compression", "Tail token budget.", minimum=1),
    ConfigField("compression.threshold_ratio", "compression_threshold_ratio", "config.yaml", 0.6, "float", "compression", "Compression threshold ratio.", minimum=0, maximum=1),
    ConfigField("memory.provider", "memory_provider", "config.yaml", "file", "str", "memory", "Built-in memory provider.", choices=MEMORY_PROVIDERS),
    ConfigField("memory.external_provider", "memory_external_provider", "config.yaml", "none", "str", "memory", "External memory provider.", choices=EXTERNAL_MEMORY_PROVIDERS),
    ConfigField("memory.review_interval", "memory_review_interval", "config.yaml", 10, "int", "memory", "Memory review interval.", minimum=0),
    ConfigField("memory.embedding.model", "memory_embedding_model", "config.yaml", "BAAI/bge-small-zh-v1.5", "str", "memory", "Embedding model."),
    ConfigField("memory.embedding.relevance_threshold", "memory_embedding_relevance_threshold", "config.yaml", 0.3, "float", "memory", "Embedding relevance threshold."),
    ConfigField("memory.embedding.max_prefetch", "memory_embedding_max_prefetch", "config.yaml", 3, "int", "memory", "Embedding prefetch limit.", minimum=1),
    ConfigField("memory.embedding.chunk_size", "memory_embedding_chunk_size", "config.yaml", 800, "int", "memory", "Embedding chunk size.", minimum=1),
    ConfigField("cron.enabled", "enable_cron", "config.yaml", False, "bool", "cron", "Enable cron scheduler."),
    ConfigField("sandbox.roots", "sandbox_roots", "config.yaml", ["./data"], "list[path]", "sandbox", "Sandbox root directories.", allow_csv=True),
    ConfigField("sandbox.blocked", "sandbox_blocked", "config.yaml", [], "list", "sandbox", "Blocked sandbox path patterns."),
    ConfigField("sandbox.bash_work_dir", "bash_work_dir", "config.yaml", "./data", "path", "sandbox", "Bash working directory."),
    ConfigField("sandbox.bash_restrict_paths", "bash_restrict_paths", "config.yaml", True, "bool", "sandbox", "Restrict bash paths."),
    ConfigField("sandbox.bash_allow_network", "bash_allow_network", "config.yaml", False, "bool", "sandbox", "Allow bash network commands."),
    ConfigField("sandbox.file_max_write_bytes", "file_max_write_bytes", "config.yaml", 100000, "int", "sandbox", "Maximum file write size.", minimum=1),
    ConfigField("sandbox.audit_enabled", "audit_enabled", "config.yaml", True, "bool", "sandbox", "Enable tool audit logging."),
    ConfigField("gateway.platform_reconnect_delays", "platform_reconnect_delays", "config.yaml", [1, 2, 5, 10, 30, 60], "list[int]", "gateway", "Platform reconnect delays.", allow_csv=True, minimum=1),
    ConfigField("gateway.platform_pending_warning_threshold", "platform_pending_warning_threshold", "config.yaml", 10, "int", "gateway", "Pending message warning threshold.", minimum=1),
    ConfigField("gateway.platform_chat_locks_maxsize", "platform_chat_locks_maxsize", "config.yaml", 64, "int", "gateway", "Gateway chat lock cache size.", minimum=1),
    ConfigField("gateway.platform_message_dedupe_max_size", "platform_message_dedupe_max_size", "config.yaml", 1024, "int", "gateway", "Gateway message dedupe cache size.", minimum=1),
    ConfigField("gateway.platform_send_max_retries", "platform_send_max_retries", "config.yaml", 2, "int", "gateway", "Platform send retry limit.", minimum=0),
    ConfigField("session.expire_days", "session_expire_days", "config.yaml", 30, "int", "session", "Session expiry in days.", minimum=0),
    ConfigField("session.override", "session_override", "config.yaml", {}, "dict", "session", "Session routing overrides."),
    ConfigField("mcp.enabled", "mcp_enabled", "config.yaml", False, "bool", "mcp", "Enable MCP."),
    ConfigField("mcp.servers", "mcp_servers", "config.yaml", [], "list", "mcp", "Configured MCP servers."),
    ConfigField("plugins.dirs", "plugins_dirs", "config.yaml", ["./plugins", "./data/plugins"], "list[path]", "plugins", "Plugin directories.", allow_csv=True),
    ConfigField("plugins.enabled", "plugins_enabled", "config.yaml", [], "list", "plugins", "Enabled plugins."),
    ConfigField("plugins.disabled", "plugins_disabled", "config.yaml", [], "list", "plugins", "Disabled plugins."),
    ConfigField("auth.enabled", "auth_enabled", "config.yaml", False, "bool", "auth", "Enable auth."),
    ConfigField("auth.admins", "auth_admins", "config.yaml", [], "list", "auth", "Admin users."),
    ConfigField("auth.allowed_users", "auth_allowed_users", "config.yaml", [], "list", "auth", "Allowed users."),
    ConfigField("profiles", "profile_map", "config.yaml/.env", {}, "dict", "profiles", "Session profile map.", env_key="PROFILES", yaml_path="profiles"),
)


CONFIG_REGISTRY = ConfigRegistry(CONFIG_FIELDS)


def config_fields_by_section() -> dict[str, list[ConfigField]]:
    return CONFIG_REGISTRY.by_section()


def config_field_by_path(path: str) -> ConfigField | None:
    return CONFIG_REGISTRY.get(path)


def config_sections() -> set[str]:
    return CONFIG_REGISTRY.sections()


def config_yaml_fields() -> tuple[ConfigField, ...]:
    return CONFIG_REGISTRY.yaml_fields()


def config_env_fields() -> tuple[ConfigField, ...]:
    return CONFIG_REGISTRY.env_fields()


def config_yaml_known_sections() -> set[str]:
    return CONFIG_REGISTRY.yaml_known_sections()


def config_yaml_known_keys_by_section() -> dict[str, set[str] | None]:
    return CONFIG_REGISTRY.yaml_known_keys_by_section()


def registry_fields_summary() -> dict[str, Any]:
    return CONFIG_REGISTRY.summary()


def registry_schema() -> dict[str, Any]:
    return CONFIG_REGISTRY.schema()


def registry_coverage(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return CONFIG_REGISTRY.coverage(config)


def validate_registry_config(config: dict[str, Any]) -> dict[str, list[str]]:
    return CONFIG_REGISTRY.validate_config(config)


def validate_registry_value(field: ConfigField, value: Any) -> dict[str, list[str]]:
    return CONFIG_REGISTRY.validate_value(field, value)


def _validate_field_value(field: ConfigField, value: Any) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    label = field.path
    value_type = field.value_type

    if field.required and _is_empty(value):
        errors.append(f"{label} 是必填配置。")
        return {"errors": errors, "warnings": warnings}
    if value is None:
        return {"errors": errors, "warnings": warnings}

    if value_type == "bool":
        if not isinstance(value, bool):
            errors.append(f"{label} 必须是 true/false。")
    elif value_type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{label} 必须是整数。")
        else:
            _validate_number_range(field, value, errors)
    elif value_type == "float":
        if not isinstance(value, int | float) or isinstance(value, bool):
            errors.append(f"{label} 必须是数字。")
        else:
            _validate_number_range(field, float(value), errors)
    elif value_type == "str":
        if not isinstance(value, str):
            errors.append(f"{label} 必须是字符串。")
        elif field.choices and value not in field.choices:
            errors.append(f"{label} 不支持: {value}，可选: {', '.join(str(item) for item in field.choices)}")
    elif value_type == "path":
        if not isinstance(value, str):
            errors.append(f"{label} 必须是路径字符串。")
    elif value_type == "dict":
        if not isinstance(value, dict):
            errors.append(f"{label} 必须是对象。")
    elif value_type.startswith("list"):
        _validate_list_value(field, value, errors)
    return {"errors": errors, "warnings": warnings}


def effective_config_snapshot(settings) -> dict[str, Any]:
    return CONFIG_REGISTRY.snapshot_from_settings(settings).as_dict()


def config_field_env_key(field: ConfigField) -> str:
    return field.env_key or field.path


def config_field_yaml_path(field: ConfigField) -> str:
    return field.yaml_path or field.path


def _path_section(path: str) -> str:
    return path.split(".", 1)[0]


def _raw_config_value(config: dict[str, Any], path: str) -> tuple[bool, Any]:
    parts = path.split(".")
    current: Any = config
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _validate_number_range(field: ConfigField, value: int | float, errors: list[str]) -> None:
    if field.minimum is not None and value < field.minimum:
        errors.append(f"{field.path} 必须大于等于 {field.minimum}。")
    if field.maximum is not None and value > field.maximum:
        errors.append(f"{field.path} 必须小于等于 {field.maximum}。")


def _validate_list_value(field: ConfigField, value: Any, errors: list[str]) -> None:
    if isinstance(value, str) and field.allow_csv:
        items: list[Any] = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        items = value
    else:
        suffix = "或逗号分隔字符串" if field.allow_csv else ""
        errors.append(f"{field.path} 必须是列表{suffix}。")
        return

    if field.value_type == "list[int]":
        for item in items:
            if not isinstance(item, int) or isinstance(item, bool):
                try:
                    item = int(str(item))
                except (TypeError, ValueError):
                    errors.append(f"{field.path} 必须只包含整数。")
                    return
            _validate_number_range(field, int(item), errors)
        return

    if field.value_type == "list[path]":
        if any(not isinstance(item, str) for item in items):
            errors.append(f"{field.path} 必须只包含字符串。")


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def _count_sources(sources: dict[str, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for source in sources.values():
        result[source] = result.get(source, 0) + 1
    return result


def _masked_attr_values(fields: tuple[dict[str, Any], ...], values: dict[str, Any]) -> dict[str, Any]:
    sensitive_attrs = {
        str(field.get("attr"))
        for field in fields
        if field.get("sensitive") and field.get("attr")
    }
    return {
        key: _masked_value(value) if key in sensitive_attrs else value
        for key, value in values.items()
    }


def _masked_raw_env(fields: tuple[dict[str, Any], ...], env: dict[str, str]) -> dict[str, str]:
    sensitive_keys = {
        str(field.get("env_key") or field.get("path"))
        for field in fields
        if field.get("sensitive") and ".env" in str(field.get("source", ""))
    }
    return {
        key: str(_masked_value(value)) if key in sensitive_keys else value
        for key, value in env.items()
    }


def _masked_value(value: Any) -> str:
    return "<set>" if bool(value) else "<unset>"


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
