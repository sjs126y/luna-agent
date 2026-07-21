"""Registry-driven configuration loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from luna_agent.config_registry import (
    CONFIG_REGISTRY,
    ConfigField,
    ConfigRegistry,
    ConfigSnapshot,
    config_field_env_key,
    config_field_yaml_path,
)


class ConfigLoaderError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class ConfigLoader:
    def __init__(
        self,
        *,
        registry: ConfigRegistry = CONFIG_REGISTRY,
        base_dir: Path | str = ".",
    ) -> None:
        self.registry = registry
        self.base_dir = Path(base_dir)

    def load(self, *, overrides: dict[str, Any] | None = None, strict: bool = True) -> ConfigSnapshot:
        overrides = overrides or {}
        raw_config, config_error = load_yaml_config(self.base_dir / "config.yaml")
        raw_env = load_env_config(self.base_dir / ".env")
        environment = {**raw_env, **os.environ}
        fields: list[dict[str, Any]] = []
        values: dict[str, Any] = {}
        attr_values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        sections: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        warnings: list[str] = []
        if config_error:
            errors.append(f"config.yaml 解析失败: {config_error}")
        auth_config = raw_config.get("auth")
        if isinstance(auth_config, dict):
            for legacy in ("admins", "allowed_users"):
                if legacy in auth_config:
                    errors.append(
                        f"auth.{legacy} 已移除，请改用按平台配置的 auth.owner_ids。"
                    )

        for field in self.registry.fields:
            source, raw_value = self._resolve_raw_value(field, raw_config, environment, overrides)
            if field.path == "profiles":
                value, field_errors = self._resolve_profiles(field, raw_config, environment, overrides)
                field_warnings: list[str] = []
                if not field_errors:
                    source = _profile_source(raw_config, environment, overrides, field)
            else:
                value, field_errors, field_warnings = convert_config_value(field, raw_value)
            if field_errors:
                source = "invalid"
                value = _default_runtime_value(field)
            errors.extend(field_errors)
            warnings.extend(field_warnings)

            item = field.as_dict(value=value, include_value=True)
            item["resolved_source"] = source
            fields.append(item)
            values[field.path] = item.get("value")
            attr_values[field.attr] = value
            sources[field.path] = source
            sections.setdefault(field.section, []).append(item)

        source_counts = _count_sources(sources)
        snapshot = ConfigSnapshot(
            fields=tuple(sorted(fields, key=lambda item: item["path"])),
            values=values,
            attr_values=attr_values,
            sources=sources,
            source_counts=source_counts,
            sections={
                section: tuple(sorted(items, key=lambda item: item["path"]))
                for section, items in sorted(sections.items())
            },
            raw_env=raw_env,
            raw_config=raw_config,
            environment=environment,
            errors=tuple(_dedupe(errors)),
            warnings=tuple(_dedupe(warnings)),
        )
        if strict and snapshot.errors:
            raise ConfigLoaderError(list(snapshot.errors))
        return snapshot

    def _resolve_raw_value(
        self,
        field: ConfigField,
        raw_config: dict[str, Any],
        raw_env: dict[str, str],
        overrides: dict[str, Any],
    ) -> tuple[str, Any]:
        if field.attr in overrides:
            return "override", overrides[field.attr]
        if field.path in overrides:
            return "override", overrides[field.path]
        if ".env" in field.source:
            env_key = config_field_env_key(field)
            if env_key in raw_env:
                return ".env", raw_env[env_key]
        if "config.yaml" in field.source:
            found, value = _raw_config_value(raw_config, config_field_yaml_path(field))
            if found:
                return "config.yaml", value
        return "default", field.default

    def _resolve_profiles(
        self,
        field: ConfigField,
        raw_config: dict[str, Any],
        raw_env: dict[str, str],
        overrides: dict[str, Any],
    ) -> tuple[dict[str, str], list[str]]:
        if field.attr in overrides or field.path in overrides:
            current_value = overrides.get(field.attr, overrides.get(field.path))
            if isinstance(current_value, dict):
                return {str(key): str(value) for key, value in current_value.items()}, []
            return {}, ["profiles 必须是对象。"]
        profiles: dict[str, str] = {}
        yaml_profiles = raw_config.get("profiles") or {}
        if isinstance(yaml_profiles, dict):
            profiles.update({str(key): str(value) for key, value in yaml_profiles.items()})
        elif "profiles" in raw_config:
            return profiles, ["profiles 必须是对象。"]
        raw_profiles_env = raw_env.get(config_field_env_key(field), "")
        if raw_profiles_env:
            try:
                env_profiles = json.loads(raw_profiles_env)
            except json.JSONDecodeError as exc:
                return profiles, [f"PROFILES 必须是 JSON 对象: {exc.msg}。"]
            if not isinstance(env_profiles, dict):
                return profiles, ["PROFILES 必须是 JSON 对象。"]
            profiles.update({str(key): str(value) for key, value in env_profiles.items()})
        return profiles, []


def load_yaml_config(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}, "config.yaml must be an object"
        return data, ""
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def load_env_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values

        return {key: value or "" for key, value in dotenv_values(path).items()}
    except Exception:
        return {}


def convert_config_value(field: ConfigField, raw_value: Any) -> tuple[Any, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    label = field.path
    value_type = field.value_type
    runtime_type = field.runtime_type or value_type

    if field.required and _is_empty(raw_value):
        return None, [f"{label} 是必填配置。"], warnings
    if raw_value is None:
        return None, errors, warnings

    try:
        if value_type == "bool":
            value = _convert_bool(raw_value, label)
        elif value_type == "int":
            value = _convert_int(raw_value, label)
        elif value_type == "float":
            value = _convert_float(raw_value, label)
        elif value_type == "str":
            value = _convert_str(raw_value, label)
        elif value_type == "path":
            value = _convert_str(raw_value, label)
        elif value_type == "dict":
            value = _convert_dict(raw_value, label)
        elif value_type == "list[int]":
            value = _convert_int_list(raw_value, label, allow_csv=field.allow_csv)
        elif value_type == "list[float]":
            value = _convert_float_list(raw_value, label, allow_csv=field.allow_csv)
        elif value_type in {"list", "list[path]"}:
            value = _convert_list(raw_value, label, allow_csv=field.allow_csv)
        else:
            value = raw_value
    except ValueError as exc:
        return None, [str(exc)], warnings

    errors.extend(_validate_converted_value(field, value))
    if runtime_type == "path":
        value = Path(value)
    elif runtime_type == "list[path]" or value_type == "list[path]":
        value = [Path(item) for item in value]
    return value, errors, warnings


def _validate_converted_value(field: ConfigField, value: Any) -> list[str]:
    errors: list[str] = []
    if field.path == "permissions.tool_approval":
        from luna_agent.security.config import tool_approval_config_errors

        errors.extend(tool_approval_config_errors(value))
    if field.path == "permissions.approval_reviewer":
        from luna_agent.security.config import approval_reviewer_config_errors

        errors.extend(approval_reviewer_config_errors(value))
    if field.value_type == "str" and field.choices and value not in field.choices:
        errors.append(f"{field.path} 不支持: {value}，可选: {', '.join(str(item) for item in field.choices)}")
    if field.value_type in {"int", "float"}:
        _validate_number_range(field, value, errors)
    if field.value_type in {"list[int]", "list[float]"}:
        for item in value:
            _validate_number_range(field, item, errors)
    if field.value_type == "list[float]" and not value:
        errors.append(f"{field.path} 至少需要一个数字。")
    return errors


def _convert_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{label} 必须是 true/false。")


def _convert_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{label} 必须是整数。")


def _convert_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是数字。")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{label} 必须是数字。")


def _convert_float_list(value: Any, label: str, *, allow_csv: bool) -> list[float]:
    values = _convert_list(value, label, allow_csv=allow_csv)
    return [_convert_float(item, label) for item in values]


def _convert_str(value: Any, label: str) -> str:
    if isinstance(value, Path):
        return str(value)
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串。")
    return value


def _convert_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象。")
    return dict(value)


def _convert_list(value: Any, label: str, *, allow_csv: bool) -> list[Any]:
    if isinstance(value, str) and allow_csv:
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        suffix = "或逗号分隔字符串" if allow_csv else ""
        raise ValueError(f"{label} 必须是列表{suffix}。")
    return [str(item) if isinstance(item, Path) else item for item in value]


def _convert_int_list(value: Any, label: str, *, allow_csv: bool) -> list[int]:
    raw_items = _convert_list(value, label, allow_csv=allow_csv)
    result: list[int] = []
    for item in raw_items:
        result.append(_convert_int(item, label))
    return result


def _default_runtime_value(field: ConfigField) -> Any:
    value, errors, _warnings = convert_config_value(field, field.default)
    if errors:
        return field.default
    return value


def _profile_source(
    raw_config: dict[str, Any],
    raw_env: dict[str, str],
    overrides: dict[str, Any],
    field: ConfigField,
) -> str:
    if field.attr in overrides or field.path in overrides:
        return "override"
    if raw_env.get(config_field_env_key(field)):
        return ".env"
    if "profiles" in raw_config:
        return "config.yaml"
    return "default"


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


def _count_sources(sources: dict[str, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for source in sources.values():
        result[source] = result.get(source, 0) + 1
    return result


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
