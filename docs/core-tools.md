<div align="center">

# Core Agent Tools

**让高频工具直接可用，让低频能力按需出现**

![Core](https://img.shields.io/badge/core-17%20tools-2EA44F)
![Bridge](https://img.shields.io/badge/tool%20bridge-3%20entries-2563EB)
![Tests](https://img.shields.io/badge/tests-1065%20passed-2EA44F)

[项目首页](../README.md) · [文档中心](README.md) · [架构说明](architecture.md) · [安全边界](capabilities-and-boundaries.md)

</div>

---

## 暴露原则

```mermaid
flowchart LR
    A[Agent turn] --> B[17 个高频核心工具]
    A --> C[tool_search / describe / call]
    C --> D[便利工具]
    C --> E[高级执行与 Workflow]
    C --> F[MCP 与插件工具]
```

Registry 继续保存全部能力，但模型每轮只接收稳定的核心 schema。低频工具通过工具桥接搜索并进入同一 Executor、Permission、Sandbox 和 Audit 管道，不会因为延迟暴露而绕过安全检查。

## 核心集合

| 领域 | 直接暴露工具 | 原因 |
| --- | --- | --- |
| 文件 | `read`、`write`、`edit`、`grep`、`glob` | 绝大多数本地任务的基础动作 |
| Shell | `bash` | 短时、受限命令入口 |
| Web | `web_search`、`web_fetch` | 外部信息的基础入口 |
| Memory | `memory`、`memory_buffer` | 长期信息和内部缓冲 |
| Skill | `skill_search`、`skill_load` | 按任务加载专业指导 |
| Agent | `sub_agent` | 单一、通用的委派原语 |
| Process | `process_start`、`process_read`、`process_wait`、`process_kill` | 长任务最常用生命周期 |

当存在延迟工具时，Registry 额外暴露 `tool_search`、`tool_describe` 和 `tool_call` 三个桥接入口。

## 按需能力

以下工具没有删除，只是不再常驻每轮 Prompt：

| 类别 | 工具示例 | 处理理由 |
| --- | --- | --- |
| 简单便利功能 | calculator、datetime、random、timer、json、weather | 低频且容易挤占模型选择空间 |
| 计划与持久任务 | todo、task | 只在用户明确需要任务管理时加载 |
| 高级执行 | execute_code、process_list、process_clear | Bash/常用进程原语已覆盖日常路径 |
| 多 Agent 组合 | sub_parallel、sub_pipeline、delegate_task、run_review | 保留能力，避免多个近义入口常驻 |
| Workflow / Worktree | workflow_*、worktree_* | 专项且部分操作风险较高 |
| 交互与附件 | clarify、confirm、artifact_from_file、response_attach | 由明确任务或系统提示触发搜索 |
| MCP / 插件 | 动态注册工具 | 数量不稳定，统一按需发现 |

## 可靠性边界

| 工具 | 当前保证 |
| --- | --- |
| `glob` | 线程扫描、目录剪枝、100 默认结果上限、10 秒和 50k 条目预算 |
| `grep` | 共用有界扫描、逐行读取、跳过二进制和大文件、50 匹配上限 |
| `read` | `offset/limit` 分页、50k 字节窗口、二进制拒绝、线程 I/O |
| `write/edit` | UTF-8 实际字节限制、同目录原子替换、超大文件编辑拒绝 |
| `bash` | 异步进程、超时与中断、持续排空输出、64 KiB 捕获上限 |
| `web_fetch` | 每次跳转前 SSRF 校验、5 次跳转上限、2 MiB 响应上限、内容类型约束 |
| 后台进程 | 异步读取，stdout/stderr 各自只保留 4k 字符尾部 |

实际事故中的 `/home/sujinsheng` 全目录 `glob('*')` 从约 379 秒降至约 0.02 秒，并在找到 100 个结果后立即停止。

## 已知边界

- `web_search` 当前依赖 Bing 页面解析并使用 DDGS fallback，不需要付费 API，但结果质量和站点结构有关。
- 延迟工具依赖 `tool_search` 的检索质量；工具描述、标签和中文别名需要持续维护。
- 工具线程超时后不能强制杀死 Python 线程，因此扫描内核自身也必须检查时间和条目预算。
- 受保护路径、精确资源授权和审计规则在核心与按需工具之间完全一致。
