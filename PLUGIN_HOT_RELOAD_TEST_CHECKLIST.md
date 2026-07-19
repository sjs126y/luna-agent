# Plugin Hot Reload Test Checklist

This checklist validates plugin installation, generation switching, rollback, runtime routing,
enable/disable, and uninstall through the same long-running Gateway process.

## Test Fixtures

- v1: `examples/plugins/hot_reload_probe_v1`
- v2: `examples/plugins/hot_reload_probe_v2`
- Both packages intentionally use the same plugin key: `examples/hot-reload-probe`
- The plugin owns `/hot-version` and the `hot_reload_probe` tool.

The probe does not use memory, network access, or persistent plugin storage.

## Before Testing

Start the branch under test from the repository root:

```bash
git switch refactor/plugin-hot-reload-runtime
uv run luna-agent serve
```

Run all `/plugins` commands in WeChat, QQ, Feishu, or the TUI connected to that process. A separate
`luna-agent plugins ...` process cannot replace capabilities inside an already-running Gateway.

Use these absolute fixture paths:

```text
/home/sujinsheng/projects/luna-agent/examples/plugins/hot_reload_probe_v1
/home/sujinsheng/projects/luna-agent/examples/plugins/hot_reload_probe_v2
```

## 1. Install v1

Send:

```text
/plugins install /home/sujinsheng/projects/luna-agent/examples/plugins/hot_reload_probe_v1
/plugins info examples/hot-reload-probe
/hot-version
```

Expected:

- install succeeds without restarting the Gateway;
- `/hot-version` returns `"version": "v1"`;
- `/plugins info` reports `runtime_state: active`;
- record the v1 `package_digest`, `generation_id`, and `runtime_instance_id`.

Then tell Luna:

```text
只调用一次 hot_reload_probe，delay_seconds=0，原样返回工具结果，不要调用其他工具。
```

Expected: the tool returns v1 and the same generation/runtime identities shown by `/hot-version`.

## 2. Update to v2

Send:

```text
/plugins install /home/sujinsheng/projects/luna-agent/examples/plugins/hot_reload_probe_v2
/plugins info examples/hot-reload-probe
/hot-version
```

Expected:

- no Gateway restart is needed;
- `/hot-version` returns `"version": "v2"`;
- both `generation_id` and `runtime_instance_id` differ from v1;
- the package digest differs from v1.

Call `hot_reload_probe` once again. It must return v2.

## 3. Roll Back to v1

Use the v1 package digest recorded in step 1:

```text
/plugins rollback examples/hot-reload-probe <v1-package-digest>
/hot-version
```

Expected:

- version returns to v1;
- generation returns to the v1 content generation;
- runtime instance is new because rollback creates a fresh runtime instance.

## 4. Disable and Enable

Send:

```text
/plugins disable examples/hot-reload-probe
/hot-version
```

Expected: disable succeeds and `/hot-version` is no longer registered. Asking Luna to find
`hot_reload_probe` must not expose the tool.

Then send:

```text
/plugins enable examples/hot-reload-probe
/hot-version
```

Expected: the command and tool are available again without restarting the Gateway.

## 5. Uninstall

Send:

```text
/plugins uninstall examples/hot-reload-probe
/plugins list
/hot-version
```

Expected:

- the active route is removed immediately;
- the plugin is absent from the active plugin list;
- `/hot-version` and `hot_reload_probe` are unavailable;
- no Gateway restart is needed.

The ordinary uninstall preserves plugin data. This probe writes no data. Use `--purge-data` only
when intentionally testing permanent data deletion.

## 6. Optional In-Flight Consistency Test

This test needs two independent sessions or users because barrier commands in one session wait for
that session's active turn.

1. Install v1.
2. In session A, ask for exactly one `hot_reload_probe` call with `delay_seconds=15`.
3. While it is sleeping, install v2 from session B.
4. Session A's in-flight result must still return v1.
5. The next call from session A must return v2.

This proves a turn keeps its leased capability snapshot while newly admitted turns use the new
snapshot. The same condition is also covered deterministically by
`tests/test_plugin_hot_reload.py::test_reload_publishes_new_routes_and_drains_old_runtime`.

## Failure Evidence

For any failure, keep these items together:

- the exact command or user message;
- the complete response;
- `/plugins info examples/hot-reload-probe` output;
- Gateway logs from five seconds before to five seconds after the operation;
- whether the Gateway had been restarted between steps.
