# Frontend Legacy Cleanup Plan

更新时间：2026-07-07

本文是前端/TUI 侧遗留清理审计清单，只记录候选项和建议处理顺序，不代表已经决定要删或要改。执行任何清理前先由用户确认；不改 git 历史、不 force push、不删除本地数据或配置。

## 原则

- 先清文档和说明，再清代码。
- 只清当前代码树，不重写历史提交。
- 保留兼容层，除非确认没有前端、配置、测试或用户流程依赖。
- 不动 `data/`、`.env`、用户私有配置、sandbox/权限/安全核心逻辑。
- classic CLI / inline TUI 的默认关系需要用户决策，不能因为“看起来旧”直接删除。

## 候选清单

### F-DOC-01：`FRONTEND_PROGRESS.md` 历史段落里仍有过期 UI 描述

- 位置：`FRONTEND_PROGRESS.md`
- 现象：
  - “最近完成”历史里仍保留旧描述，例如顶部 meter 曾显示 cache 摘要、activity badge。
  - 当前状态段已经修正为：context meter 只显示 context usage + 最近一轮 token，cache/activity 不常驻顶部。
- 风险：低，文档清理。
- 建议：
  - 保留最近完成时间线，但把已反转的旧 UI 描述标为“历史记录，当前已改为...”，或者压缩到 archive。
  - 不影响代码。
- 决策：待确认。

### F-DOC-02：`src/personal_agent/tui/README.md` 和 README 对 inline TUI 成熟度表述不完全一致

- 位置：
  - `src/personal_agent/tui/README.md`
  - `README.md`
  - `config.yaml`
- 现象：
  - TUI README 写“通过 `--ui inline` 启用；默认仍是 classic UI”。
  - README 已把 inline TUI 描述为项目当前主要前端能力之一。
  - `config.yaml` 当前仍是 `agent.ui: "classic"`。
- 风险：低到中。
  - 如果只改说明，风险低。
  - 如果改默认 UI，从 `classic` 改 `inline`，属于行为变化，需要用户明确同意并做真实终端验证。
- 建议：
  - 先确认产品决策：classic 是否继续默认。
  - 若 classic 继续默认：README/TUI README 应统一说“inline 已稳定可用，但默认仍 classic”。
  - 若 inline 改默认：另开任务，改配置默认、CLI help、测试和手测清单。
- 决策：待确认。

### F-DOC-03：归档计划文档仍有早期设计假设

- 位置：
  - `docs/archive/TUI_PLAN.md`
  - `docs/archive/FRONTEND_ROADMAP.md`
  - `src/personal_agent/tui/__init__.py`
  - `src/personal_agent/tui/README.md`
- 现象：
  - 归档文档里仍写旧设想：旧 renderer 全程默认、复用 prompt_toolkit `Completer`、Phase checklist 等。
  - 当前 inline TUI 已经改为自管 slash menu，避免 prompt_toolkit completer 改写输入。
- 风险：低。
- 建议：
  - 不删除 archive。
  - 在归档文件顶部加明显说明：“历史计划，仅供背景，不是当前权威方案”。
  - 当前权威仍为 `FRONTEND_PROGRESS.md`、`BACKEND_INTERFACE.md`、`docs/frontend_decisions.md`。
- 决策：待确认。

### F-DOC-04：`FRONTEND_INTERFACE_REQUIREMENTS.md` 里有已实现需求和本地绝对路径示例

- 位置：`FRONTEND_INTERFACE_REQUIREMENTS.md`
- 现象：
  - Activity Runtime 需求已经由后端实现并被前端消费，但需求文档仍保留大量字段级契约。
  - 示例里有本地路径：`/home/sujinsheng/projects/Personal-Agent-backend`。
- 风险：低。
- 建议：
  - 把已实现 Activity 需求移动到“已满足/归档”段，保留一行指向 `BACKEND_INTERFACE.md`。
  - 示例路径改成 `/path/to/project` 或 `<workspace>`，减少本机路径残留。
- 决策：待确认。

### F-CONFIG-01：仓库跟踪的 `config.yaml` 含本机/Windows/WSL sandbox roots

- 位置：`config.yaml`
- 现象：
  - `sandbox.roots` 包含 `/home/sujinsheng/projects/Personal-Agent`、`/mnt/c/Users/MR/Desktop/...` 等本地路径。
- 风险：中。
  - 这可能是当前用户真实运行配置，不能直接清。
  - 但作为仓库默认配置，路径偏本机化，后续对外或长期维护不理想。
- 建议：
  - 决定 `config.yaml` 是“可运行本机配置”还是“模板配置”。
  - 如果作为模板：改为相对路径或示例路径，并把本地路径移到用户本地配置。
  - 如果作为本机配置：保留，不做清理。
- 决策：待确认。

### F-CODE-01：`UIState` 保留 cache usage 字段，但顶部 UI 不再消费

- 位置：
  - `src/personal_agent/tui/state.py`
  - `src/personal_agent/tui/renderer.py`
  - `tests/test_tui_renderer.py`
- 现象：
  - `cache_hit_tokens`、`cache_miss_tokens`、`cache_write_tokens`、`cache_read_tokens`、`cache_hit_rate` 仍由 renderer 写入 state。
  - 顶部 meter 已按用户偏好移除 cache 常驻展示。
- 风险：中。
- 建议：
  - 暂时保留，作为未来 `/usage`、context breakdown、doctor/debug panel 的数据基础。
  - 如果确认未来 TUI 不展示 cache，另开任务删除字段和对应测试。
- 决策：待确认。

### F-CODE-02：`UIState.activity_total/activity_attention` 只记录，不再常驻显示

- 位置：
  - `src/personal_agent/tui/state.py`
  - `src/personal_agent/tui/app.py`
  - `tests/test_tui_app.py`
  - `tests/test_tui_layout.py`
- 现象：
  - `/activity` payload 会更新 `activity_total` / `activity_attention`。
  - 顶部 meter 已移除 activity badge。
  - 当前这些字段不再驱动可见 UI。
- 风险：中。
- 建议：
  - 如果未来需要全局 activity 指示器，保留。
  - 如果 `/activity` 只作为命令输出，不需要全局状态，删除 `_update_activity_state()`、state 字段和相关测试。
- 决策：待确认。

### F-CODE-03：classic `TerminalRenderer` 与 inline TUI 两套终端前端并存

- 位置：
  - `src/personal_agent/cli_shell.py`
  - `src/personal_agent/tui/`
  - `src/personal_agent/cli.py`
  - `config.yaml`
- 现象：
  - classic `TerminalRenderer` 仍是默认 UI。
  - classic 仍有自己的 `SlashCompleter`、Ctrl+O overlay、状态栏和工具 trace。
  - inline TUI 有另一套 slash menu、confirm panel、activity formatting、meter。
- 风险：高。
  - 这是兼容层，不是简单遗留。
  - 删除或改默认会影响用户真实 CLI 使用。
- 建议：
  - 先由用户决定是否长期支持 classic。
  - 若保留：只做文档统一，不清代码。
  - 若废弃：先标 deprecated，一个版本周期后再删，并补迁移说明。
- 决策：待确认。

### F-CODE-04：`scripts/spike_inline.py` 是早期真实终端 spike

- 位置：
  - `scripts/spike_inline.py`
  - `src/personal_agent/tui/README.md`
  - `src/personal_agent/tui/layout.py`
- 现象：
  - 当前 TUI README 和 layout 注释仍引用 spike 作为 Phase 0 验证。
  - 这个脚本不是测试，也不是运行时入口。
- 风险：低。
- 建议：
  - 保留但标为“历史 spike”，或者移动到 `docs/archive/` / `scripts/archive/`。
  - 如果移动，需要同步 README/layout 注释。
- 决策：待确认。

### F-CODE-05：`src/personal_agent/tui/README.md` 仍写“后端如果暴露 confirm= 回调”

- 位置：`src/personal_agent/tui/README.md`
- 现象：
  - README 仍说“后端如果暴露 `confirm=` 回调，app 会自动传入”。
  - 当前后端已经支持 confirm callback，inline TUI 也已经接入。
- 风险：低。
- 建议：
  - 改成“后端已支持 confirm callback，inline TUI 会传入 `confirm_tool`”。
- 决策：待确认。

## 建议执行顺序

### 第一批：低风险文档清理

可选目标：

- F-DOC-01
- F-DOC-03
- F-DOC-04
- F-CODE-05 的 README 描述部分

验证：

```bash
uv run pytest tests/test_docs.py -q
git diff --check
```

### 第二批：配置/默认 UI 决策

可选目标：

- F-DOC-02
- F-CONFIG-01
- F-CODE-03 的默认 UI 决策

需要用户先确认：

- `classic` 是否继续默认。
- `config.yaml` 是本机配置还是模板配置。

### 第三批：代码状态字段清理

可选目标：

- F-CODE-01
- F-CODE-02

需要用户先确认：

- TUI 是否未来需要 cache/context breakdown 面板。
- `/activity` 是否需要全局状态提示，还是只保留命令输出。

验证：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
python -m compileall -q src/personal_agent/tui
git diff --check
```

## 不建议清理

- 不清 `data/`、`.env`、用户本地运行数据。
- 不删 classic CLI，除非先确认弃用策略。
- 不删 sandbox、permission、execution guard 相关兼容代码。
- 不删 `BACKEND_INTERFACE.md`、`FRONTEND_PROGRESS.md`、`CODEX_HANDOFF.md`，最多归档/压缩过期段落。
- 不动 git 历史。
