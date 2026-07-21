# Plugin Architecture Debt

状态：延后处理；当前插件 generation/runtime 重构已经收口，没有需要立即启动的大规模重构。

最后更新：2026-07-21

本文记录插件架构中已经确认、但当前不值得立即处理的技术债和稳定化工作。它不是新一轮重构计划。开始其中任何事项前，应先确认对应触发条件已经出现，并保持现有架构边界。

## 已确定的架构边界

后续修改不得无理由推翻以下结论：

- Tool、Skill、Hook、Command、Workflow、Platform、MCP 和 Memory manager 保持各自功能内聚，不聚合成一个巨型插件快照对象。
- `RegistrationTransaction` 暂存 candidate generation 的宿主可见注册，插件 `register()` 不直接发布线上状态。
- `GenerationCoordinator` 是 generation 状态迁移和候选提交的唯一入口。
- `CapabilityRouter` 和 `CapabilityStore` 是运行时能力路由与不可变快照的事实来源；兼容 registry 是提交后的宿主投影。
- `ActiveSupervisor` 管理主动任务生命周期，`WorkerSupervisor` 管理隔离 Worker、恢复、退避和熔断。
- Memory provider 和已有 Platform route 是 boot-scoped；不能热切换时明确报告 `pending_restart`。
- 不采用一次性大改。兼容层只允许缩小，不允许继续吸收新的职责。

## 问题清单

| ID | 问题 | 当前优先级 | 触发处理的条件 |
| --- | --- | --- | --- |
| PA-01 | `LoadedPlugin` 兼容门面 | P2 | 新代码持续依赖代理字段，或字段迁移开始阻碍类型检查和模型演进 |
| PA-02 | 全局兼容 registry | P2 | 再次出现绕过事务的直接注册，或 Snapshot/registry 同步成本明显上升 |
| PA-03 | `PluginManager` 编排面偏大 | P3 | 某组逻辑出现独立变化频率、独立状态或三个以上独立调用方 |
| PA-04 | 长时间运行与故障注入不足 | P1 | 远端 CI 首次稳定后立即安排，不等待线上故障 |
| PA-05 | Windows AppContainer 持续验证不足 | P1 | CI workflow 推送后验证 hosted runner；发版前验证真实 Windows 11 |
| PA-06 | CI、版本和 Release 治理未闭环 | P1/P2 | 首次 CI 通过后配置 required checks；准备公开安装包前补 Release gate |
| PA-07 | 功能面持续扩张风险 | 持续约束 | 新需求需要同时修改三个以上核心子系统时进行架构评审 |

## PA-01：LoadedPlugin 兼容门面

### 当前情况

`LoadedPlugin` 已不再直接拥有全部插件状态。真实状态分布在：

- `PluginDefinition`：manifest、启用状态、管理错误。
- `PluginGeneration`：runtime identity、后端、Worker、active、数据 revision 和注册事务。
- `WorkerRuntimeStatus` / `ActiveRuntimeStatus`：监督状态。
- `GenerationRegistrations`：当前 generation 的注册名称。
- `PluginView`：只读管理和诊断投影。

为了兼容旧调用，`LoadedPlugin` 仍通过动态 property 暴露 `worker_state`、`active_runner`、`tools_registered`、`runtime_instance_id` 等字段。这使旧代码可以继续工作，但会隐藏真正的状态所有者，并削弱静态类型检查。

### 当前保护

- 架构边界测试限制 `runtime_state` 的写入位置。
- Worker 和 active 生命周期已经从 Manager 移入各自 Supervisor。
- 查询接口开始使用只读 `PluginView` 和安全摘要。
- 兼容 property 只做转发，不允许承载新的恢复、发布或路由逻辑。

### 后续迁移顺序

1. 禁止新增 `LoadedPlugin` 动态代理字段。
2. doctor、CLI、前端和只读查询统一消费 `PluginView` 或稳定 payload。
3. 生命周期代码显式使用 `plugin.generation`。
4. 启用状态和管理错误显式使用 `plugin.definition`。
5. 为剩余代理字段统计调用方，逐项迁移并删除。
6. 最终将 `LoadedPlugin` 缩为显式 `PluginHandle`，或者在无调用方后移除。

### 完成标准

- 不再通过动态 `setattr()` 构造公开状态字段。
- `PluginView` 覆盖全部外部诊断需求。
- Coordinator、Router 和 Supervisor 不依赖兼容代理写入状态。
- 删除兼容层不会改变插件加载、重载或诊断 payload。

## PA-02：全局兼容 Registry

### 当前情况

`tool_registry`、`skill_registry`、`workflow_registry`、`platform_registry` 和 Memory provider registry 仍被宿主既有执行路径使用。热重载后，它们不再允许由 candidate 直接修改：`RegistrationTransaction` 先暂存，提交时才更新兼容 registry，并与 Capability Snapshot 一起回滚。

剩余风险是插件或宿主新代码直接 import registry 并调用 `register()`，从而绕过 candidate、冲突检查和事务回滚。

### 后续工作

- 增加静态边界测试，禁止插件入口和 `PluginRuntimeContext` 之外的新直接 registry 写入。
- 将宿主消费者逐步迁移到 capability view 或窄接口查询。
- registry 最终只保留为 Router 发布后的派生适配器。
- 在 SDK 和插件文档中明确：插件注册只能通过 `ctx.register`。

### 完成标准

- 所有插件注册都能追溯到一个 `RegistrationTransaction` 或明确的 core dynamic source。
- candidate 准备阶段不会改变任何线上 registry。
- registry 不再被当作插件 generation 的事实来源。

## PA-03：PluginManager 编排面偏大

### 当前情况

`PluginManager` 仍连接发现、依赖、加载、安装、重载、回滚、MCP、数据 revision、查询和 Supervisor。它已经不直接拥有 Worker 恢复、active watch task 或 capability route 状态，因此当前属于较大的 composition/orchestration facade，而不是第二个全能 runtime。

### 不应立即做的事情

- 不按文件行数机械拆分类。
- 不再创建一个包装 `PluginManager`、只做转发的新 Manager。
- 不把 Coordinator、Router、Supervisor 的状态重新聚合回 Manager。

### 允许提取的条件

只有某组逻辑满足至少一项时才提取：

- 有独立状态和生命周期。
- 有三个以上独立调用方。
- 经常在不修改加载主链路的情况下单独变化。
- 能形成窄接口并拥有独立测试。

未来可能的边界包括 `PluginCatalog`（发现和定义）与 `PluginPackageService`（安装、版本和回滚）。是否提取由实际修改压力决定。

## PA-04：Soak 与 Fault Injection

现有测试覆盖单次加载、回滚、Worker 恢复、active quiesce 和 lease 排空，但不能替代长时间序列验证。

### 建议场景

- 同一插件连续 reload 100～500 次。
- Turn 长时间持有旧 Snapshot lease，同时持续发布新 generation。
- Worker 在 preparing、publishing、active ready 和 shutdown 阶段分别退出。
- 并发触发 install、reload、rollback、disable 和 uninstall，验证 operation lock。
- 在 registry activate、data commit、snapshot callback、Worker spawn 和 sandbox cleanup 处注入异常。
- 模拟磁盘空间不足、数据 revision 写失败和环境 lease 无法释放。
- Gateway 关闭时同时存在 Worker recovery 和 active restart。

### 必须观察的指标

- Worker、recovery task、watch task 和环境 lease 数量回到基线。
- retained snapshot 和旧 generation 不无限增长。
- module namespace、临时目录和数据 revision 能被回收。
- 文件描述符、线程和内存占用没有持续增长。
- shutdown 在配置的超时时间内结束。

2026-07-21 本地验证时观察到一组从前一日遗留的 pytest/Worker 进程会干扰新的异步 SQLite 测试。当前只作为环境现象记录，不能在未从干净进程复现前认定为 Runtime 泄漏。后续 soak 应包含被中断测试的进程树清理断言。

这类测试适合 nightly CI 或发版门禁，不应拖慢每个普通 PR。

## PA-05：Windows AppContainer 持续验证

原生 smoke 已验证文件、网络、子进程、stdio、Job Object 和 profile cleanup。当前缺少的是持续证据，而不是启动器主体实现。

### 验证层次

1. PR/main：GitHub `windows-latest` 执行 `scripts/windows_plugin_appcontainer_smoke.py`。
2. Release：真实 Windows 11 环境执行同一 smoke。
3. 如果 hosted runner 不允许 AppContainer：使用专用 self-hosted Windows runner，不得设置 `continue-on-error` 或静默降级到 `process-only`。

### 完成标准

- Windows CI 连续稳定通过。
- 失败时能区分 runner 策略限制、ACL 错误、profile 残留和 Worker 协议错误。
- Release 前至少有一次真实 Windows 11 结果。

## PA-06：CI、版本和 Release 治理

`.github/workflows/ci.yml` 已存在，但只有推送到远端并成功运行后才形成真实门禁。

### CI 收口

- 首次 Linux full test 与 Windows AppContainer smoke 通过。
- main branch protection 将两个 job 设置为 required checks。
- 禁止用跳过测试或 `continue-on-error` 绕过安全失败。
- 保持 Action commit SHA、Python、uv 和 `uv.lock` 可复现。

### 后续 Release gate

- 构建宿主和 Plugin SDK wheel。
- 在全新虚拟环境安装 wheel，而不是复用源码 editable install。
- 运行 `luna-agent doctor`、插件加载和最小 Gateway smoke。
- 版本 tag、变更说明和 artifact 对应同一 commit。
- Release 可以保留人工批准；不得自动部署个人配置、密钥和真实平台会话。

## PA-07：功能面扩张治理

项目已经同时包含 Agent、Conversation、Tool/Security、Memory、MCP、Gateway、Delivery、Artifact、Plugin SDK、热重载、主动插件和进程沙箱。新增能力经常会跨越多个层。

### 约束

- 新插件优先使用现有 SDK，不为单个插件向宿主加入专用分支。
- 新生命周期必须声明唯一 owner。
- 新运行状态必须进入 diagnostics，并有清理路径。
- 新前端字段同时更新 `BACKEND_INTERFACE.md`。
- 一个需求需要同时修改三个以上核心子系统时，先做边界评审。
- 在稳定化阶段，优先减少兼容层和补组合测试，不继续增加新的核心子系统。

## 推荐处理顺序

1. 推送并跑通首次跨平台 CI，配置 required checks。
2. 增加 generation reload、Worker crash 和 shutdown 的 nightly soak/fault-injection。
3. 禁止扩大 `LoadedPlugin` 与直接 registry 写入，并渐进迁移调用方。
4. 准备公开 Release 时补 wheel 安装验证和人工发布门禁。
5. 只有出现真实变化压力时才继续拆分 `PluginManager`。

在上述触发条件出现之前，本文件只作为维护边界和未来工作入口，不代表当前迭代必须继续修改插件架构。
