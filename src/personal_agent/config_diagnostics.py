"""Configuration diagnostics shared by init and doctor."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from personal_agent.config_loader import ConfigLoader
from personal_agent.config_registry import (
    CONFIG_REGISTRY,
    registry_coverage,
    registry_fields_summary,
    registry_schema,
    validate_registry_config,
)
from personal_agent.security.modes import MODE_PRESETS


KNOWN_TOP_LEVEL_KEYS = {
    "agent",
    "agents",
    "auth",
    "compression",
    "gateway",
    "cron",
    "execution",
    "mcp",
    "memory",
    "multimodal",
    "permissions",
    "plugins",
    "profiles",
    "sandbox",
    "session",
    "storage",
    "toolsets",
}

KNOWN_SECTION_KEYS: dict[str, set[str] | None] = {
    "agent": {"max_iterations", "max_tool_calls_per_turn"},
    "agents": {"max_concurrent_runs", "max_tool_calls", "max_tokens", "history_limit"},
    "auth": {"enabled", "admins", "allowed_users"},
    "compression": {"engine", "model", "max_tokens", "tail_token_budget", "threshold_ratio"},
    "gateway": {
        "platform_reconnect_delays",
        "platform_message_dedupe_max_size",
        "delivery_max_attempts",
    },
    "cron": {"enabled"},
    "execution": {"mode"},
    "mcp": {"enabled", "servers"},
    "memory": {"external_provider", "review", "llm", "embedding", "qdrant", "providers"},
    "multimodal": {
        "enabled",
        "image_mode",
        "audio_mode",
        "video_mode",
        "file_mode",
        "native_fallback",
        "text_extract_max_chars",
        "text_extract_pdf_max_pages",
        "image_text_mode",
        "image_text_cache",
        "image_text_max_chars",
        "image_text_provider",
        "image_text_model",
        "image_text_prompt",
        "ocr_endpoint",
        "ocr_timeout_seconds",
        "ocr_language",
    },
    "permissions": {
        "grant_ttl_minutes",
        "confirm_timeout_seconds",
        "tool_approval",
    },
    "plugins": {"dirs", "enabled", "disabled", "config"},
    "profiles": None,
    "sandbox": {
        "roots",
        "blocked",
        "bash_work_dir",
        "bash_restrict_paths",
        "bash_allow_network",
        "process_backend",
        "file_max_write_bytes",
        "audit_enabled",
    },
    "session": {"expire_days", "override"},
    "storage": {"data_dir", "log_level"},
    "toolsets": {"enabled"},
}

DEPRECATED_TOP_LEVEL_KEYS = {
    "platform": "平台配置已插件化，平台 secret 放到 .env，启用项由插件和 env 决定。",
    "platforms": "平台配置已插件化，平台 secret 放到 .env，启用项由插件和 env 决定。",
}

PROVIDER_REQUIRED_ENV = {
    "deepseek": ["LLM_API_KEY"],
    "openai": ["LLM_API_KEY"],
    "anthropic": ["LLM_API_KEY"],
    "openrouter": ["LLM_API_KEY"],
    "xai": ["LLM_API_KEY"],
}

VALID_LLM_PROVIDERS = set(PROVIDER_REQUIRED_ENV)
VALID_LLM_API_MODES = {"auto", "chat_completions", "anthropic_messages", "responses", "codex_responses"}
VALID_COMPRESSION_ENGINES = {"compressor", "simple", "none", "off", "disabled"}
VALID_EXTERNAL_MEMORY_PROVIDERS = {"none", "fallback", "lumora", "mem0"}
VALID_EXECUTION_MODES = set(MODE_PRESETS)

PLATFORM_ENV = {
    "telegram": ["TELEGRAM_BOT_TOKEN"],
    "feishu": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    "wechat": ["WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID", "WEIXIN_USER_ID"],
    "qq": ["QQ_BOT_BASE_URL"],
}

PLATFORM_HINTS = {
    "telegram": "填写 TELEGRAM_BOT_TOKEN，或从 plugins.enabled 移除 platforms/telegram。",
    "feishu": "填写 FEISHU_APP_ID/FEISHU_APP_SECRET，或从 plugins.enabled 移除 platforms/feishu。",
    "wechat": "填写 WEIXIN_TOKEN/WEIXIN_ACCOUNT_ID/WEIXIN_USER_ID，或完成微信凭据初始化。",
    "qq": "填写 QQ_BOT_BASE_URL，确保 OneBot HTTP 服务地址可用。",
}

MCP_SERVER_KEYS = {
    "name",
    "transport",
    "command",
    "args",
    "env",
    "url",
    "headers_env",
    "enabled",
    "connect_timeout_seconds",
    "call_timeout_seconds",
    "allow_insecure_http",
    "allow_private_network",
    "allow_network",
    "max_tools",
    "max_tool_pages",
    "max_schema_bytes",
    "max_result_chars",
    "max_artifact_bytes",
    "artifact_roots",
    "artifact_extensions",
}
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def build_config_report(base_dir: Path | str = ".") -> dict[str, Any]:
    base = Path(base_dir)
    config_path = base / "config.yaml"
    env_path = base / ".env"
    env_example_path = base / ".env.example"
    config, config_error = _read_yaml(config_path)
    registry_snapshot = ConfigLoader(base_dir=base).load(strict=False)
    env = registry_snapshot.environment

    llm_provider = str(env.get("LLM_PROVIDER") or "deepseek").strip()
    llm_api_mode = str(env.get("LLM_API_MODE") or "auto").strip()
    llm_max_tokens = str(env.get("LLM_MAX_TOKENS") or "4096").strip()
    llm_context_window = str(env.get("LLM_CONTEXT_WINDOW") or "0").strip()
    llm_reasoning_effort = str(env.get("LLM_REASONING_EFFORT") or "").strip()
    required_llm_env = PROVIDER_REQUIRED_ENV.get(llm_provider, ["LLM_API_KEY"])
    missing_llm_env = [name for name in required_llm_env if not env.get(name)]
    llm_base_url = str(env.get("LLM_BASE_URL") or "")
    llm_model = str(env.get("LLM_MODEL") or "")

    directories = _directory_report(base, config)
    unknown_keys = [
        key for key in sorted(config)
        if key not in _known_top_level_keys() and key not in DEPRECATED_TOP_LEVEL_KEYS
    ]
    deprecated_keys = [
        {"key": key, "message": DEPRECATED_TOP_LEVEL_KEYS[key]}
        for key in sorted(config)
        if key in DEPRECATED_TOP_LEVEL_KEYS
    ]
    validation = _validate_config(config)
    registry_validation = validate_registry_config(config)
    coverage = registry_coverage(config)
    env_validation = _validate_env(
        llm_provider=llm_provider,
        llm_api_mode=llm_api_mode,
        llm_max_tokens=llm_max_tokens,
        llm_context_window=llm_context_window,
    )
    mcp_servers = _mcp_server_report(config)
    path_warnings = _path_warnings(directories)
    platform_env = _platform_env_report(config, env)

    errors: list[str] = []
    warnings: list[str] = []
    if config_error:
        errors.append(f"config.yaml 解析失败: {config_error}")
    if not config_path.exists():
        warnings.append("缺少 config.yaml。")
    if not env_path.exists():
        warnings.append("缺少 .env。")
    if missing_llm_env:
        warnings.append(f"缺少 LLM 环境变量: {', '.join(missing_llm_env)}")
    if not llm_base_url:
        warnings.append("LLM_BASE_URL 未设置，将依赖 provider 默认值。")
    if not llm_model:
        warnings.append("LLM_MODEL 未设置，将使用默认模型。")
    for item in directories:
        if not item["exists"] and item["required"] and item.get("portable", True):
            warnings.append(f"目录不存在: {item['path']} ({item['kind']})")
    for key in unknown_keys:
        warnings.append(f"未知 config 顶层配置: {key}")
    for key in validation["unknown_nested_keys"]:
        warnings.append(f"未知 config 配置: {key}")
    for item in deprecated_keys:
        warnings.append(f"已废弃配置 {item['key']}: {item['message']}")
    for item in platform_env:
        if item["enabled"] and item["missing_env"]:
            warnings.append(
                f"平台 {item['name']} 缺少环境变量: {', '.join(item['missing_env'])}"
            )

    errors.extend(env_validation["errors"])
    errors.extend(validation["errors"])
    errors.extend(registry_validation["errors"])
    errors.extend(registry_snapshot.errors)
    errors.extend(mcp_servers["errors"])
    warnings.extend(env_validation["warnings"])
    warnings.extend(validation["warnings"])
    warnings.extend(registry_validation["warnings"])
    warnings.extend(registry_snapshot.warnings)
    warnings.extend(path_warnings)
    warnings.extend(mcp_servers["warnings"])

    errors = _dedupe(errors)
    warnings = _dedupe(warnings)
    migration_hints = _migration_hints(
        config=config,
        unknown_keys=unknown_keys,
        unknown_nested_keys=validation["unknown_nested_keys"],
        deprecated_keys=deprecated_keys,
        platform_env=platform_env,
    )
    recommended_commands = _recommended_commands(
        config_path=config_path,
        env_path=env_path,
        env_example_path=env_example_path,
        missing_llm_env=missing_llm_env,
        directories=directories,
        errors=errors,
        warnings=warnings,
        path_warnings=path_warnings,
    )
    next_steps = _next_steps(
        config_path,
        env_path,
        env_example_path,
        missing_llm_env,
        directories,
        errors,
        warnings,
    )
    return {
        "ok": not errors and not warnings,
        "base_dir": str(base),
        "files": {
            "config": {"path": str(config_path), "exists": config_path.exists(), "error": config_error},
            "env": {"path": str(env_path), "exists": env_path.exists()},
            "env_example": {"path": str(env_example_path), "exists": env_example_path.exists()},
        },
        "env": {
            "llm_provider": llm_provider,
            "llm_api_mode": llm_api_mode,
            "llm_api_key_set": bool(env.get("LLM_API_KEY")),
            "llm_base_url": llm_base_url,
            "llm_base_url_set": bool(llm_base_url),
            "llm_model": llm_model,
            "llm_model_set": bool(llm_model),
            "llm_max_tokens": llm_max_tokens,
            "llm_context_window": llm_context_window,
            "llm_reasoning_effort": llm_reasoning_effort,
            "missing_llm_env": missing_llm_env,
            "platforms": platform_env,
        },
        "directories": directories,
        "mcp_servers": mcp_servers["servers"],
        "unknown_keys": unknown_keys,
        "unknown_nested_keys": validation["unknown_nested_keys"],
        "deprecated_keys": deprecated_keys,
        "migration_hints": migration_hints,
        "registry_fields": registry_fields_summary(),
        "registry_schema": registry_schema(),
        "registry_snapshot": registry_snapshot.as_dict(),
        "registry_coverage": coverage,
        "registry_validation_errors": registry_validation["errors"],
        "registry_validation_warnings": registry_validation["warnings"],
        "registry_loader_errors": list(registry_snapshot.errors),
        "registry_loader_warnings": list(registry_snapshot.warnings),
        "registry_source_counts": dict(registry_snapshot.source_counts),
        "recommended_commands": recommended_commands,
        "errors": errors,
        "warnings": warnings,
        "path_warnings": path_warnings,
        "validation_errors": (
            validation["errors"]
            + registry_validation["errors"]
            + list(registry_snapshot.errors)
            + env_validation["errors"]
            + mcp_servers["errors"]
        ),
        "next_steps": next_steps,
    }


def ensure_config_dirs(base_dir: Path | str) -> list[str]:
    base = Path(base_dir)
    config, _ = _read_yaml(base / "config.yaml")
    paths = [
        item["path"]
        for item in _directory_report(base, config)
        if item.get("portable", True)
        and (item["required"] or item["kind"] in {"plugin_dir", "system_dir"})
    ]
    created: list[str] = []
    for value in paths:
        path = Path(value)
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if not existed:
            created.append(str(path))
    return created


def _read_yaml(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}, "config.yaml must be an object"
        return data, ""
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _validate_env(
    *,
    llm_provider: str,
    llm_api_mode: str,
    llm_max_tokens: str,
    llm_context_window: str,
) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if llm_provider not in VALID_LLM_PROVIDERS:
        errors.append(
            f"LLM_PROVIDER 不支持: {llm_provider}，可选: {', '.join(sorted(VALID_LLM_PROVIDERS))}"
        )
    if llm_api_mode not in VALID_LLM_API_MODES:
        errors.append(
            f"LLM_API_MODE 不支持: {llm_api_mode}，可选: {', '.join(sorted(VALID_LLM_API_MODES))}"
        )
    try:
        max_tokens = int(llm_max_tokens)
    except ValueError:
        errors.append("LLM_MAX_TOKENS 必须是正整数。")
    else:
        if max_tokens <= 0:
            errors.append("LLM_MAX_TOKENS 必须大于 0。")
        elif max_tokens < 256:
            warnings.append("LLM_MAX_TOKENS 很小，可能导致回复被截断。")
    try:
        context_window = int(llm_context_window)
    except ValueError:
        errors.append("LLM_CONTEXT_WINDOW 必须是非负整数，0 表示自动推断。")
    else:
        if context_window < 0:
            errors.append("LLM_CONTEXT_WINDOW 必须大于等于 0，0 表示自动推断。")
    return {"errors": errors, "warnings": warnings}


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    unknown_nested_keys: list[str] = []

    sections: dict[str, dict[str, Any]] = {}
    for section, allowed_keys in _known_section_keys().items():
        raw = config.get(section)
        if raw is None:
            sections[section] = {}
            continue
        if not isinstance(raw, dict):
            errors.append(f"配置 {section} 必须是对象。")
            sections[section] = {}
            continue
        sections[section] = raw
        if allowed_keys is not None:
            unknown_nested_keys.extend(
                f"{section}.{key}" for key in sorted(raw) if key not in allowed_keys
            )

    _positive_int(sections["agent"], "max_iterations", "agent.max_iterations", errors)
    _positive_int(
        sections["agent"],
        "max_tool_calls_per_turn",
        "agent.max_tool_calls_per_turn",
        errors,
    )
    for key in ("max_concurrent_runs", "max_tool_calls", "max_tokens", "history_limit"):
        _positive_int(sections["agents"], key, f"agents.{key}", errors)

    _string_value(sections["storage"], "data_dir", "storage.data_dir", errors)
    _string_value(sections["storage"], "log_level", "storage.log_level", errors)

    _string_list_or_csv(sections["plugins"], "dirs", "plugins.dirs", errors)
    _string_list(sections["plugins"], "enabled", "plugins.enabled", errors)
    _string_list(sections["plugins"], "disabled", "plugins.disabled", errors)
    _string_list(sections["toolsets"], "enabled", "toolsets.enabled", errors)

    compression = sections["compression"]
    _enum_value(compression, "engine", "compression.engine", VALID_COMPRESSION_ENGINES, errors)
    _string_value(compression, "model", "compression.model", errors)
    _positive_int(compression, "max_tokens", "compression.max_tokens", errors)
    _positive_int(compression, "tail_token_budget", "compression.tail_token_budget", errors)
    _ratio_value(compression, "threshold_ratio", "compression.threshold_ratio", errors)

    gateway = sections["gateway"]
    _int_list_or_csv(gateway, "platform_reconnect_delays", "gateway.platform_reconnect_delays", errors)
    _positive_int(
        gateway,
        "platform_message_dedupe_max_size",
        "gateway.platform_message_dedupe_max_size",
        errors,
    )
    _positive_int(
        gateway,
        "delivery_max_attempts",
        "gateway.delivery_max_attempts",
        errors,
    )

    memory = sections["memory"]
    _enum_value(
        memory,
        "external_provider",
        "memory.external_provider",
        VALID_EXTERNAL_MEMORY_PROVIDERS,
        errors,
    )
    providers = memory.get("providers")
    if providers is not None and not isinstance(providers, dict):
        errors.append("memory.providers 必须是对象。")
    elif isinstance(providers, dict):
        lumora = providers.get("lumora")
        if lumora is not None and not isinstance(lumora, dict):
            errors.append("memory.providers.lumora 必须是对象。")
        elif isinstance(lumora, dict):
            for name in ("embedding", "vector", "keyword", "fusion", "reranker"):
                selection = lumora.get(name)
                if selection is not None and not isinstance(selection, dict):
                    errors.append(f"memory.providers.lumora.{name} 必须是对象。")
                elif isinstance(selection, dict):
                    _string_value(
                        selection,
                        "provider",
                        f"memory.providers.lumora.{name}.provider",
                        errors,
                    )

    _bool_value(sections["cron"], "enabled", "cron.enabled", errors)

    execution = sections["execution"]
    _execution_mode_value(execution, "mode", "execution.mode", errors)

    permissions = sections["permissions"]
    _range_int(permissions, "grant_ttl_minutes", "permissions.grant_ttl_minutes", 1, 10080, errors)
    _range_int(permissions, "confirm_timeout_seconds", "permissions.confirm_timeout_seconds", 10, 600, errors)
    if "tool_approval" in permissions:
        from personal_agent.security.config import tool_approval_config_errors

        errors.extend(tool_approval_config_errors(permissions["tool_approval"]))

    sandbox = sections["sandbox"]
    _string_list_or_csv(sandbox, "roots", "sandbox.roots", errors)
    _string_list_or_csv(sandbox, "read_roots", "sandbox.read_roots", errors)
    _string_list(sandbox, "blocked", "sandbox.blocked", errors)
    _string_value(sandbox, "bash_work_dir", "sandbox.bash_work_dir", errors)
    for key in ("bash_restrict_paths", "bash_allow_network", "audit_enabled"):
        _bool_value(sandbox, key, f"sandbox.{key}", errors)
    _enum_value(
        sandbox,
        "process_backend",
        "sandbox.process_backend",
        {"auto", "bwrap", "legacy"},
        errors,
    )
    _positive_int(sandbox, "file_max_write_bytes", "sandbox.file_max_write_bytes", errors)

    session = sections["session"]
    _non_negative_int(session, "expire_days", "session.expire_days", errors)
    _dict_value(session, "override", "session.override", errors)

    mcp = sections["mcp"]
    _bool_value(mcp, "enabled", "mcp.enabled", errors)
    if "servers" in mcp and not isinstance(mcp["servers"], list):
        errors.append("mcp.servers 必须是列表。")

    auth = sections["auth"]
    _bool_value(auth, "enabled", "auth.enabled", errors)
    _string_list(auth, "admins", "auth.admins", errors)
    _string_list(auth, "allowed_users", "auth.allowed_users", errors)

    return {
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "unknown_nested_keys": sorted(_dedupe(unknown_nested_keys)),
    }


def _known_top_level_keys() -> set[str]:
    return set(KNOWN_TOP_LEVEL_KEYS) | CONFIG_REGISTRY.yaml_known_sections()


def _known_section_keys() -> dict[str, set[str] | None]:
    result: dict[str, set[str] | None] = {
        section: None if keys is None else set(keys)
        for section, keys in CONFIG_REGISTRY.yaml_known_keys_by_section().items()
    }
    for section, keys in KNOWN_SECTION_KEYS.items():
        if keys is None:
            result[section] = None
            continue
        if section in result and result[section] is None:
            continue
        result.setdefault(section, set()).update(keys)
    return result


def _directory_report(base: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    storage = config.get("storage") if isinstance(config.get("storage"), dict) else {}
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    sandbox = config.get("sandbox") if isinstance(config.get("sandbox"), dict) else {}

    data_dir = storage.get("data_dir", "./data")
    plugin_dirs = _string_or_list(plugins.get("dirs", ["./plugins", "./data/plugins"]))
    sandbox_roots = _string_or_list(sandbox.get("roots", ["./data"]))
    sandbox_read_roots = _string_or_list(sandbox.get("read_roots", []))
    bash_work_dir = sandbox.get("bash_work_dir", "./data")

    result = [
        _dir_item(base, "data_dir", data_dir, required=True),
        _dir_item(base, "system_dir", str(_path_for_child(base, data_dir, "system")), required=True),
        _dir_item(base, "bash_work_dir", bash_work_dir, required=True),
    ]
    result.extend(_dir_item(base, "plugin_dir", item, required=False) for item in plugin_dirs)
    result.extend(_dir_item(base, "sandbox_root", item, required=True) for item in sandbox_roots)
    result.extend(
        _dir_item(base, "sandbox_read_root", item, required=True)
        for item in sandbox_read_roots
    )
    return result


def _mcp_server_report(config: dict[str, Any]) -> dict[str, Any]:
    mcp = config.get("mcp") if isinstance(config.get("mcp"), dict) else {}
    mcp_enabled = bool(mcp.get("enabled", False))
    raw_servers = mcp.get("servers", [])
    servers: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    if raw_servers is None:
        raw_servers = []
    if not isinstance(raw_servers, list):
        return {"servers": servers, "errors": ["mcp.servers 必须是列表。"], "warnings": []}

    seen_names: set[str] = set()
    for index, raw in enumerate(raw_servers):
        label = f"mcp.servers[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} 必须是对象。")
            continue
        unknown = sorted(key for key in raw if key not in MCP_SERVER_KEYS)
        if unknown:
            warnings.append(f"{label} 包含未知字段: {', '.join(unknown)}")
        command = str(raw.get("command") or "")
        url = str(raw.get("url") or "")
        transport = str(raw.get("transport") or ("streamable_http" if url and not command else "stdio"))
        name = str(raw.get("name") or command or url or f"server-{index}")
        enabled = bool(raw.get("enabled", True))
        command_found = bool(command and shutil.which(command))
        duplicate_name = name in seen_names
        seen_names.add(name)
        servers.append({
            "index": index,
            "name": name,
            "transport": transport,
            "command": command,
            "url": url,
            "enabled": enabled,
            "command_found": command_found,
            "missing_command": transport == "stdio" and not bool(command),
            "missing_url": transport == "streamable_http" and not bool(url),
            "duplicate_name": duplicate_name,
            "unknown_keys": unknown,
            "allow_insecure_http": bool(raw.get("allow_insecure_http", False)),
            "allow_private_network": bool(raw.get("allow_private_network", False)),
            "allow_network": bool(raw.get("allow_network", False)),
        })
        for key in ("allow_insecure_http", "allow_private_network", "allow_network"):
            if key in raw and not isinstance(raw[key], bool):
                errors.append(f"{label}.{key} 必须是 true/false。")
        for key in (
            "max_tools",
            "max_tool_pages",
            "max_schema_bytes",
            "max_result_chars",
            "max_artifact_bytes",
        ):
            if key in raw and (
                not isinstance(raw[key], int)
                or isinstance(raw[key], bool)
                or raw[key] <= 0
            ):
                errors.append(f"{label}.{key} 必须是正整数。")
        for key in ("artifact_roots", "artifact_extensions"):
            if key in raw and (
                not isinstance(raw[key], list)
                or any(not isinstance(value, str) for value in raw[key])
            ):
                errors.append(f"{label}.{key} 必须是字符串列表。")
        if transport not in {"stdio", "streamable_http"}:
            errors.append(f"MCP 服务器 {name} 使用不支持的 transport: {transport}")
        elif mcp_enabled and enabled and transport == "stdio" and not command:
            errors.append(f"MCP 服务器 {name} 缺少 command。")
        elif mcp_enabled and enabled and transport == "stdio" and not command_found:
            warnings.append(f"MCP 服务器 {name} 的命令不可用: {command}")
        elif mcp_enabled and enabled and transport == "streamable_http" and not url:
            errors.append(f"MCP 服务器 {name} 缺少 url。")
        elif mcp_enabled and enabled and transport == "streamable_http":
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                errors.append(f"MCP 服务器 {name} 的 url 必须是 http(s) URL。")
            elif parsed.scheme == "http" and not raw.get("allow_insecure_http", False):
                errors.append(
                    f"MCP 服务器 {name} 使用 HTTP；如确认可信需显式设置 allow_insecure_http: true。"
                )
        if duplicate_name:
            errors.append(f"MCP 服务器名称重复: {name}")

    return {"servers": servers, "errors": _dedupe(errors), "warnings": _dedupe(warnings)}


def _platform_env_report(config: dict[str, Any], env: dict[str, str]) -> list[dict[str, Any]]:
    enabled_platforms = _enabled_platforms(config, env)
    result = []
    for name, required in PLATFORM_ENV.items():
        enabled = name in enabled_platforms
        env_set = [item for item in required if env.get(item)]
        missing_env = [item for item in required if not env.get(item)] if enabled else []
        result.append({
            "name": name,
            "key": f"platforms/{name}",
            "enabled": enabled,
            "required_env": required,
            "env_set": env_set,
            "missing_env": missing_env,
            "configured": enabled and not missing_env,
            "status": _platform_env_status(enabled, env_set, missing_env),
            "hint": PLATFORM_HINTS.get(name, ""),
        })
    return result


def _platform_env_status(enabled: bool, env_set: list[str], missing_env: list[str]) -> str:
    if enabled and missing_env:
        return "incomplete"
    if enabled:
        return "ready"
    if env_set:
        return "env-present"
    return "idle"


def _enabled_platforms(config: dict[str, Any], env: dict[str, str]) -> set[str]:
    enabled: set[str] = set()
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    for key in plugins.get("enabled", []) or []:
        if isinstance(key, str) and key.startswith("platforms/"):
            enabled.add(key.split("/", 1)[1])
    for name, required in PLATFORM_ENV.items():
        if any(env.get(item) for item in required):
            enabled.add(name)
    return enabled


def _next_steps(
    config_path: Path,
    env_path: Path,
    env_example_path: Path,
    missing_llm_env: list[str],
    directories: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
) -> list[str]:
    steps: list[str] = []
    if not config_path.exists():
        steps.append("运行 personal-agent init 生成 config.yaml。")
    if not env_example_path.exists():
        steps.append("运行 personal-agent init 生成 .env.example。")
    if not env_path.exists():
        steps.append("根据 .env.example 创建 .env，或运行 personal-agent init --copy-env。")
    if missing_llm_env:
        steps.append(f"在 .env 中填写 {', '.join(missing_llm_env)}。")
    if any(not item["exists"] and item["required"] and item.get("portable", True) for item in directories):
        steps.append("运行 personal-agent init --fix-dirs 创建基础目录。")
    if any(not item.get("portable", True) for item in directories):
        steps.append("修正 config.yaml 中的 Windows/反斜杠路径，WSL 下建议使用 /mnt/c/... 或 Linux 路径。")
    if errors:
        steps.append("先修复上面的配置错误，再启动 Personal Agent。")
    elif warnings:
        steps.append("修复上面的配置警告后再运行 personal-agent doctor。")
    if not steps:
        steps.append("配置检查通过，可以运行 personal-agent chat 或 personal-agent serve。")
    return steps


def _migration_hints(
    *,
    config: dict[str, Any],
    unknown_keys: list[str],
    unknown_nested_keys: list[str],
    deprecated_keys: list[dict[str, str]],
    platform_env: list[dict[str, Any]],
) -> list[str]:
    hints: list[str] = []
    llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    llm_mapping = {
        "provider": "LLM_PROVIDER",
        "api_key": "LLM_API_KEY",
        "base_url": "LLM_BASE_URL",
        "model": "LLM_MODEL",
        "api_mode": "LLM_API_MODE",
        "max_tokens": "LLM_MAX_TOKENS",
    }
    platforms = config.get("platforms") if isinstance(config.get("platforms"), dict) else {}
    memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}

    for item in deprecated_keys:
        key = item["key"]
        if key in {"platform", "platforms"}:
            hints.append(
                "删除顶层 platform/platforms；平台 secret 放到 .env，平台插件 key 使用 platforms/telegram 等。"
            )
            for name in sorted(platforms):
                hints.append(
                    f"旧配置 platforms.{name} 请改为 plugins.enabled 添加 platforms/{name}，secret 放入 .env。"
                )
    if isinstance(llm, dict):
        for old_key, env_name in llm_mapping.items():
            if old_key in llm:
                hints.append(f"旧配置 llm.{old_key} 请迁移到 .env 的 {env_name}。")
    if unknown_keys:
        hints.append(f"确认或移除未知顶层配置: {', '.join(unknown_keys)}。")
    if unknown_nested_keys:
        hints.append(f"确认或移除未知配置: {', '.join(unknown_nested_keys)}。")
    for item in platform_env:
        if item["enabled"] and item["missing_env"]:
            hints.append(
                f"平台 {item['name']} 已启用但缺少 env: {', '.join(item['missing_env'])}。"
            )
    return _dedupe(hints)


def _recommended_commands(
    *,
    config_path: Path,
    env_path: Path,
    env_example_path: Path,
    missing_llm_env: list[str],
    directories: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
    path_warnings: list[str],
) -> list[str]:
    commands: list[str] = []
    if not config_path.exists() or not env_example_path.exists():
        commands.append("personal-agent init")
    if not env_path.exists():
        if env_example_path.exists():
            commands.append("cp .env.example .env")
        commands.append("personal-agent init --copy-env")
    if any(not item["exists"] and item["required"] and item.get("portable", True) for item in directories):
        commands.append("personal-agent init --fix-dirs")
    if missing_llm_env:
        commands.append("编辑 .env，填写 LLM_API_KEY")
    if path_warnings:
        commands.append("编辑 config.yaml，修正 Windows/WSL 路径")
    if errors:
        commands.append("personal-agent init --check")
    if warnings:
        commands.append("personal-agent doctor")
    return _dedupe(commands)


def _path_warnings(directories: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for item in directories:
        style = item.get("path_style")
        if item.get("portable", True):
            continue
        if style == "windows_drive":
            warnings.append(
                f"{item['kind']} 使用 Windows 盘符路径: {item['raw']}；WSL/Linux 下建议改成 /mnt/c/... 或 Linux 路径。"
            )
        elif style == "unc":
            warnings.append(
                f"{item['kind']} 使用 UNC 路径: {item['raw']}；当前环境可能无法直接访问。"
            )
        elif style == "backslash":
            warnings.append(
                f"{item['kind']} 使用反斜杠路径: {item['raw']}；当前环境建议使用正斜杠路径。"
            )
    return _dedupe(warnings)


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def _string_or_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _resolve(base: Path, value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def _path_for_child(base: Path, value: Any, child: str) -> str:
    style = _path_style(value)
    if os.name != "nt" and style in {"windows_drive", "unc", "backslash"}:
        return str(value)
    return str(_resolve(base, value) / child)


def _dir_item(base: Path, kind: str, raw: Any, *, required: bool) -> dict[str, Any]:
    style = _path_style(raw)
    portable = _portable_path_style(style)
    path = Path(str(raw)) if not portable else _resolve(base, raw)
    return {
        "kind": kind,
        "raw": str(raw),
        "path": str(path),
        "exists": path.exists() if portable else False,
        "required": required,
        "path_style": style,
        "portable": portable,
    }


def _path_style(value: Any) -> str:
    text = str(value)
    if _WINDOWS_DRIVE_RE.match(text):
        return "windows_drive"
    if text.startswith("\\\\"):
        return "unc"
    if text.startswith("/mnt/") and len(text) > 7 and text[6] == "/":
        return "wsl_mount"
    if os.name != "nt" and "\\" in text:
        return "backslash"
    return "native"


def _portable_path_style(style: str) -> bool:
    if os.name == "nt":
        return True
    return style not in {"windows_drive", "unc", "backslash"}


def _positive_int(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    value = section[key]
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{label} 必须是正整数。")
    elif value <= 0:
        errors.append(f"{label} 必须大于 0。")


def _non_negative_int(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    value = section[key]
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{label} 必须是非负整数。")
    elif value < 0:
        errors.append(f"{label} 必须大于等于 0。")


def _range_int(
    section: dict[str, Any],
    key: str,
    label: str,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> None:
    if key not in section:
        return
    value = section[key]
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{label} 必须是整数。")
    elif value < minimum or value > maximum:
        errors.append(f"{label} 必须在 {minimum} 到 {maximum} 之间。")


def _ratio_value(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    value = section[key]
    if not isinstance(value, int | float) or isinstance(value, bool):
        errors.append(f"{label} 必须是 0 到 1 之间的数字。")
    elif value <= 0 or value > 1:
        errors.append(f"{label} 必须大于 0 且小于等于 1。")


def _enum_value(
    section: dict[str, Any],
    key: str,
    label: str,
    choices: set[str],
    errors: list[str],
) -> None:
    if key not in section:
        return
    value = section[key]
    if not isinstance(value, str) or value not in choices:
        errors.append(f"{label} 不支持: {value}，可选: {', '.join(sorted(choices))}")


def _execution_mode_value(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    _enum_value(section, key, label, VALID_EXECUTION_MODES, errors)


def _bool_value(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    if not isinstance(section[key], bool):
        errors.append(f"{label} 必须是 true/false。")


def _string_value(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    if not isinstance(section[key], str):
        errors.append(f"{label} 必须是字符串。")


def _dict_value(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    if not isinstance(section[key], dict):
        errors.append(f"{label} 必须是对象。")


def _string_list(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    value = section[key]
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{label} 必须是字符串列表。")


def _string_list_or_csv(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    value = section[key]
    if isinstance(value, str):
        return
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{label} 必须是字符串列表或逗号分隔字符串。")


def _int_list_or_csv(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key not in section:
        return
    value = section[key]
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        items = value
    else:
        errors.append(f"{label} 必须是正整数列表或逗号分隔字符串。")
        return

    if not items:
        errors.append(f"{label} 不能为空。")
        return

    for item in items:
        try:
            number = int(item)
        except (TypeError, ValueError):
            errors.append(f"{label} 必须只包含正整数。")
            return
        if number <= 0:
            errors.append(f"{label} 必须只包含正整数。")
            return
