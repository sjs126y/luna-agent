# Codex 交接记录

更新时间：2026-07-05 01:25 CST

## 当前状态

- 当前分支：`feature/config-management`
- 基线：`main` / `feature/backend-upgrade` 在 `1b2b276 [codex] add platform message envelopes`
- 当前 HEAD：`e104240 [codex] organize config field declarations`
- 这一轮目标：配置整理从旁路诊断推进到主加载链路，并整理字段声明结构
- 当前结论：配置管理阶段性完成，可以先收住；后续除非要做 init 模板生成、diagnostics 去重或插件配置，否则不必继续深挖配置

## 本轮完成内容

### v1 — 配置 registry 快照

- 新增 `ConfigField` 和 `CONFIG_FIELDS`，把核心配置字段集中登记。
- 新增 effective config snapshot，doctor 可以看到 Settings 的有效配置。
- 保留原 `Settings` 加载方式，registry 只作为诊断/展示的事实源雏形。
- commit：`c247743 [codex] add config registry snapshot`

### v2 — registry 校验与诊断接入

- `ConfigField` 增加基础元数据：
  - `minimum`
  - `maximum`
  - `choices`
  - `allow_csv`
  - `required`
  - `template_default`
- 增加 registry coverage、schema/field summary、基础 value validation。
- `build_config_report()` 接入：
  - `registry_coverage`
  - `registry_validation_errors`
  - `registry_validation_warnings`
- `doctor --section config` 增强 grouped effective config 展示。
- commit：`93aeae4 [codex] expand config registry diagnostics`

### v3 — ConfigRegistry API 与 ConfigSnapshot

- 新增 `ConfigRegistry` 类：
  - `get(path)`
  - `get_by_attr(attr)`
  - `by_section()`
  - `yaml_fields()`
  - `env_fields()`
  - `schema()`
  - `coverage()`
  - `validate_config()`
  - `snapshot_from_settings()`
- 新增 `ConfigSnapshot` 雏形。
- 保留旧 helper 函数作为 wrapper，避免影响已有调用。
- 字段模型预留：
  - `owner`
  - `namespace`
  - `plugin_key`
- `config_diagnostics` 输出 `registry_schema`。
- commit：`0633a72 [codex] add config registry api`

### v4 — ConfigLoader 接管 Settings 主链路

- 新增 `src/personal_agent/config_loader.py`。
- `ConfigLoader` 由 `CONFIG_REGISTRY` 驱动读取：
  - `.env`
  - `config.yaml`
  - explicit overrides
  - defaults
- 优先级固定为：
  - `overrides > .env > config.yaml > default`
- `Settings()` 改为：
  - `ConfigLoader.load(strict=True)`
  - 将 `snapshot.attr_values` 写入实例属性
  - 保留 `raw_env`、`raw_config`、`cron_jobs_path`、`execution_policy`
- `ConfigSnapshot` 增强：
  - `values`
  - `attr_values`
  - `sources`
  - `source_counts`
  - `raw_env`
  - `raw_config`
  - `errors`
  - `warnings`
- `ConfigSnapshot.as_dict()` 对敏感字段脱敏；运行时 typed snapshot 仍保留真实 secret 给 `Settings` 使用。
- 支持 `PROFILES` JSON 覆盖/合并 `config.yaml profiles`。
- `build_config_report()` 接入：
  - `registry_snapshot`
  - `registry_loader_errors`
  - `registry_loader_warnings`
  - `registry_source_counts`
- commit：`270619b [codex] route settings through config loader`

### 声明整理 — config field declarations

- `CONFIG_FIELDS` 从巨型 tuple 拆成按域函数：
  - `_execution_fields()`
  - `_llm_fields()`
  - `_platform_env_fields()`
  - `_agent_fields()`
  - `_storage_fields()`
  - `_toolset_fields()`
  - `_compression_fields()`
  - `_memory_fields()`
  - `_cron_fields()`
  - `_sandbox_fields()`
  - `_gateway_fields()`
  - `_session_fields()`
  - `_mcp_fields()`
  - `_plugin_fields()`
  - `_auth_fields()`
  - `_profile_fields()`
- 增加轻量 helper：
  - `_yaml_field`
  - `_env_field`
  - `_mixed_field`
- 不改 loader、Settings、doctor 行为。
- 新增测试锁住关键字段顺序和 metadata。
- commit：`e104240 [codex] organize config field declarations`

## 当前配置管理形态

### 核心事实源

- `src/personal_agent/config_registry.py`
  - `ConfigField`
  - `ConfigRegistry`
  - `ConfigSnapshot`
  - `CONFIG_FIELDS`
  - `CONFIG_REGISTRY`

新增核心配置的标准入口是增加一个 `ConfigField`，例如：

```python
_yaml_field(
    "gateway.max_payload_bytes",
    "gateway_max_payload_bytes",
    1048576,
    "int",
    "gateway",
    "Maximum outbound platform payload bytes.",
    minimum=1,
)
```

然后使用方直接读：

```python
settings.gateway_max_payload_bytes
```

### 主加载链路

- `src/personal_agent/config_loader.py`
  - `ConfigLoader`
  - `ConfigLoaderError`
  - `load_yaml_config`
  - `load_env_config`
  - `convert_config_value`

`Settings()` 已经不再手写逐段读取配置，而是从 loader snapshot 生成。

### 诊断展示

- `src/personal_agent/config_diagnostics.py`
  - 已接入 registry schema、loader snapshot、source counts、loader errors/warnings
- `src/personal_agent/cli.py`
  - `format_config_report()` 展示 schema version、schema fields、source counts 等

## 关键测试

- `tests/test_config_registry.py`
  - registry API
  - schema stability
  - sensitive masking
  - field order / metadata
- `tests/test_config_loader.py`
  - defaults
  - env/yaml/override 优先级
  - Path/list/path/int/bool 转换
  - PROFILES 合并
  - strict/non-strict errors
  - Settings 接入 loader
- `tests/test_config_diagnostics.py`
  - config report 接入 registry snapshot / schema / source counts

## 已验证

最近一次全量验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：

```text
591 passed
```

## 注意事项

- 测试会修改 `src/personal_agent/skills/builtin/.usage.json`，提交前必须恢复。
- `LLM_API_MODE` 没有在 registry loader 层加 choices 限制，因为测试和 transport registry 允许自定义 mode；语义诊断仍在 doctor 层处理。
- `ConfigSnapshot.as_dict()` 会脱敏 secret；typed snapshot 的 `attr_values` 保留真实值供 runtime 使用。
- 当前不读取 `os.environ`，保持项目既有行为：只读取 `.env` 文件。
- 插件动态注册配置暂未做；用户也判断短期不太可能推进。

## 后续可选方向

当前配置整理可以先收住。以后如需继续，建议顺序：

1. `init` 模板从 registry/schema 生成或至少部分生成。
2. `config_diagnostics.py` 手写规则逐步去重，减少与 loader 的重复校验。
3. 前端/配置页消费 `registry_schema()`。
4. 插件配置 namespace 真正接入 registry（低优先级）。

## 最近提交

```text
e104240 [codex] organize config field declarations
270619b [codex] route settings through config loader
0633a72 [codex] add config registry api
93aeae4 [codex] expand config registry diagnostics
c247743 [codex] add config registry snapshot
1b2b276 [codex] add platform message envelopes
```
