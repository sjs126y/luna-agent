"""Configuration diagnostics shared by init and doctor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


KNOWN_TOP_LEVEL_KEYS = {
    "agent",
    "agents",
    "auth",
    "compression",
    "cron",
    "mcp",
    "memory",
    "plugins",
    "profiles",
    "sandbox",
    "session",
    "storage",
    "toolsets",
}

DEPRECATED_TOP_LEVEL_KEYS = {
    "platform": "平台配置已插件化，平台 secret 放到 .env，启用项由插件和 env 决定。",
    "platforms": "平台配置已插件化，平台 secret 放到 .env，启用项由插件和 env 决定。",
    "llm": "LLM secret 和模型配置请使用 .env 中的 LLM_*。",
}

PROVIDER_REQUIRED_ENV = {
    "deepseek": ["LLM_API_KEY"],
    "openai": ["LLM_API_KEY"],
    "anthropic": ["LLM_API_KEY"],
    "openrouter": ["LLM_API_KEY"],
}

PLATFORM_ENV = {
    "telegram": ["TELEGRAM_BOT_TOKEN"],
    "feishu": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    "wechat": ["WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID", "WEIXIN_USER_ID"],
}


def build_config_report(base_dir: Path | str = ".") -> dict[str, Any]:
    base = Path(base_dir)
    config_path = base / "config.yaml"
    env_path = base / ".env"
    env_example_path = base / ".env.example"
    config, config_error = _read_yaml(config_path)
    env = _read_env(env_path)

    llm_provider = str(env.get("LLM_PROVIDER") or "deepseek")
    required_llm_env = PROVIDER_REQUIRED_ENV.get(llm_provider, ["LLM_API_KEY"])
    missing_llm_env = [name for name in required_llm_env if not env.get(name)]
    llm_base_url = str(env.get("LLM_BASE_URL") or "")
    llm_model = str(env.get("LLM_MODEL") or "")

    directories = _directory_report(base, config)
    unknown_keys = [
        key for key in sorted(config)
        if key not in KNOWN_TOP_LEVEL_KEYS and key not in DEPRECATED_TOP_LEVEL_KEYS
    ]
    deprecated_keys = [
        {"key": key, "message": DEPRECATED_TOP_LEVEL_KEYS[key]}
        for key in sorted(config)
        if key in DEPRECATED_TOP_LEVEL_KEYS
    ]
    platform_env = _platform_env_report(config, env)

    warnings: list[str] = []
    if config_error:
        warnings.append(f"config.yaml 解析失败: {config_error}")
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
        if not item["exists"] and item["required"]:
            warnings.append(f"目录不存在: {item['path']} ({item['kind']})")
    for key in unknown_keys:
        warnings.append(f"未知 config 顶层配置: {key}")
    for item in deprecated_keys:
        warnings.append(f"已废弃配置 {item['key']}: {item['message']}")
    for item in platform_env:
        if item["enabled"] and item["missing_env"]:
            warnings.append(
                f"平台 {item['name']} 缺少环境变量: {', '.join(item['missing_env'])}"
            )

    migration_hints = _migration_hints(
        unknown_keys=unknown_keys,
        deprecated_keys=deprecated_keys,
        platform_env=platform_env,
    )
    recommended_commands = _recommended_commands(
        config_path=config_path,
        env_path=env_path,
        env_example_path=env_example_path,
        missing_llm_env=missing_llm_env,
        directories=directories,
        warnings=warnings,
    )
    next_steps = _next_steps(
        config_path,
        env_path,
        env_example_path,
        missing_llm_env,
        directories,
        warnings,
    )
    return {
        "ok": not warnings,
        "base_dir": str(base),
        "files": {
            "config": {"path": str(config_path), "exists": config_path.exists(), "error": config_error},
            "env": {"path": str(env_path), "exists": env_path.exists()},
            "env_example": {"path": str(env_example_path), "exists": env_example_path.exists()},
        },
        "env": {
            "llm_provider": llm_provider,
            "llm_api_key_set": bool(env.get("LLM_API_KEY")),
            "llm_base_url_set": bool(llm_base_url),
            "llm_model_set": bool(llm_model),
            "missing_llm_env": missing_llm_env,
            "platforms": platform_env,
        },
        "directories": directories,
        "unknown_keys": unknown_keys,
        "deprecated_keys": deprecated_keys,
        "migration_hints": migration_hints,
        "recommended_commands": recommended_commands,
        "warnings": warnings,
        "next_steps": next_steps,
    }


def ensure_config_dirs(base_dir: Path | str) -> list[str]:
    base = Path(base_dir)
    config, _ = _read_yaml(base / "config.yaml")
    paths = [
        item["path"]
        for item in _directory_report(base, config)
        if item["required"] or item["kind"] in {"plugin_dir", "system_dir"}
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


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values

        return {key: value or "" for key, value in dotenv_values(path).items()}
    except Exception:
        return {}


def _directory_report(base: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    storage = config.get("storage") if isinstance(config.get("storage"), dict) else {}
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    sandbox = config.get("sandbox") if isinstance(config.get("sandbox"), dict) else {}

    data_dir = _resolve(base, storage.get("data_dir", "./data"))
    plugin_dirs = _string_or_list(plugins.get("dirs", ["./plugins", "./data/plugins"]))
    sandbox_roots = _string_or_list(sandbox.get("roots", ["./data"]))
    bash_work_dir = sandbox.get("bash_work_dir", "./data")

    result = [
        _dir_item("data_dir", data_dir, required=True),
        _dir_item("system_dir", data_dir / "system", required=True),
        _dir_item("bash_work_dir", _resolve(base, bash_work_dir), required=True),
    ]
    result.extend(_dir_item("plugin_dir", _resolve(base, item), required=False) for item in plugin_dirs)
    result.extend(_dir_item("sandbox_root", _resolve(base, item), required=True) for item in sandbox_roots)
    return result


def _platform_env_report(config: dict[str, Any], env: dict[str, str]) -> list[dict[str, Any]]:
    enabled_platforms = _enabled_platforms(config, env)
    result = []
    for name, required in PLATFORM_ENV.items():
        enabled = name in enabled_platforms
        result.append({
            "name": name,
            "enabled": enabled,
            "required_env": required,
            "missing_env": [item for item in required if not env.get(item)] if enabled else [],
        })
    return result


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
    if any(not item["exists"] and item["required"] for item in directories):
        steps.append("运行 personal-agent init --fix-dirs 创建基础目录。")
    if warnings:
        steps.append("修复上面的配置警告后再运行 personal-agent doctor。")
    if not steps:
        steps.append("配置检查通过，可以运行 personal-agent chat 或 personal-agent serve。")
    return steps


def _migration_hints(
    *,
    unknown_keys: list[str],
    deprecated_keys: list[dict[str, str]],
    platform_env: list[dict[str, Any]],
) -> list[str]:
    hints: list[str] = []
    for item in deprecated_keys:
        key = item["key"]
        if key == "llm":
            hints.append("将顶层 llm 配置迁移到 .env 的 LLM_PROVIDER/LLM_API_KEY/LLM_BASE_URL/LLM_MODEL。")
        elif key in {"platform", "platforms"}:
            hints.append(
                "删除顶层 platform/platforms；平台 secret 放到 .env，平台插件 key 使用 platforms/telegram 等。"
            )
    if unknown_keys:
        hints.append(f"确认或移除未知顶层配置: {', '.join(unknown_keys)}。")
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
    warnings: list[str],
) -> list[str]:
    commands: list[str] = []
    if not config_path.exists() or not env_example_path.exists():
        commands.append("personal-agent init")
    if not env_path.exists():
        if env_example_path.exists():
            commands.append("cp .env.example .env")
        commands.append("personal-agent init --copy-env")
    if any(not item["exists"] and item["required"] for item in directories):
        commands.append("personal-agent init --fix-dirs")
    if missing_llm_env:
        commands.append("编辑 .env，填写 LLM_API_KEY")
    if warnings:
        commands.append("personal-agent doctor")
    return _dedupe(commands)


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


def _dir_item(kind: str, path: Path, *, required: bool) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": str(path),
        "exists": path.exists(),
        "required": required,
    }
