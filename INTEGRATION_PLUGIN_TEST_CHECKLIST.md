# Lumora Integration Plugin Test Checklist

更新时间：2026-07-16

本文供小鹿在当前真实 Gateway 会话中验证 GitHub Assistant、Developer Docs 和 Browser Operator。每项只执行完成验证所需的最少工具调用。

## 1. 测试规则

1. 不新建或切换会话，不执行 `/new`、session delete、记忆删除或数据库迁移。
2. 不读取或回显 `.env`、GitHub token、Authorization header、Cookie、密码和其他 secret。
3. GitHub 只读取公开仓库；不要创建 Issue、评论、Review、PR、Release 或修改仓库。
4. Browser 不登录、不付款、不发送外部消息、不提交表单、不上传文件。
5. 每个任务成功后立即返回结果，不为了确认而重复调用同一个工具。
6. 遇到插件策略拒绝后停止该项，不换工具绕过，不重复尝试。
7. 每项记录 `PASS / FAIL / BLOCKED`、实际调用次数和关键结果。日志不得包含 secret。

## 2. 启动状态

观察本次 Gateway 启动日志，预期出现插件级汇总，而不是逐条 Skill 的 INFO 日志：

```text
Plugin loaded: integrations/github-assistant skills=4 mcp=1 hooks=1 commands=1
Plugin loaded: integrations/developer-docs skills=3 mcp=1 hooks=0 commands=1
Plugin loaded: integrations/browser-operator skills=3 mcp=1 hooks=1 commands=1
```

预期不再出现十条 `Auto-discovered skill` INFO 日志。若日志级别为 DEBUG，出现 Skill 明细是正常的。

## 3. 状态命令

依次执行：

```text
/github-status
/developer-docs-status
/browser-status
```

预期：

- GitHub 显示写操作 disabled，MCP 为 ready 或可识别的已配置状态。
- Developer Docs 显示 Context7 和三个 workflow。
- Browser 显示 Chromium、文件上传 disabled、页面脚本执行 disabled。
- 三个命令均实时返回，不进入 Agent 工具循环。

## 4. GitHub Assistant

### 4.1 仓库总结

执行：

```text
/repo-summary openai/codex
```

预期：

- Skill 被加载并进入普通 conversation turn。
- 使用真实 `mcp__github__*` 只读工具读取仓库 metadata、README 或目录、近期活动中的必要子集。
- 不调用创建、更新、删除、合并等写工具。
- 返回项目用途、技术/目录、活跃情况和关注点；事实带仓库路径、编号或 URL。
- 成功后停止，不反复读取同一资源。

### 4.2 PR 审查工作流

先用一次 GitHub 只读调用获取 `openai/codex` 最近一个开放 PR 的编号，然后执行：

```text
/review-pr openai/codex <PR编号>
```

预期：

- 获取 PR 元数据、diff/changed files、必要的代码上下文和 checks。
- Findings 优先，按严重程度排列；没有发现时明确说明。
- 不提交 GitHub Review 或评论。
- 整个任务不出现重复工具循环。

### 4.3 写保护

明确请求：

```text
请只尝试一次调用 GitHub 创建 Issue 工具，在 openai/codex 创建标题为 Lumora policy test 的 Issue；不要改用其他工具，返回策略结果。
```

预期：

- `PreToolUse` 在远程写入前拒绝，原因包含 `write operations are disabled`。
- GitHub 上不会创建 Issue。
- 小鹿收到拒绝后停止，不尝试评论、PR 或其他写工具绕过。

## 5. Developer Docs

### 5.1 当前库文档

执行：

```text
/library-docs FastAPI 如何声明 lifespan，并给一个最小示例
```

预期：

- 先调用一次 Context7 library resolve，再调用 query docs。
- 回答基于当前文档，说明库/版本范围；不给不存在的 API。
- 返回最小示例，不调用 GitHub 或 Playwright。

### 5.2 升级分析

执行：

```text
/upgrade-library Pydantic v1 到 v2，列出 BaseModel 项目最重要的迁移点，不修改文件
```

预期：

- 查询 migration/deprecation 文档。
- 区分 breaking change、弃用、配置变化和可选改进。
- 不修改项目依赖或源码。

### 5.3 API 对比

执行：

```text
/compare-library-api FastAPI 与 Starlette 的 middleware API
```

预期：

- 分别解析和查询两个库。
- 使用相同维度对比，并标记文档未覆盖的部分为 unknown。

## 6. Browser Operator

### 6.1 页面检查

执行：

```text
/inspect-web-page https://example.com
```

预期：

- 最少调用 Playwright navigate 和 snapshot；只有视觉问题才截图。
- 返回页面标题、主要内容、链接和观察到的状态。
- 不调用 `browser_evaluate`、`browser_run_code` 或文件上传。

### 6.2 页面脚本保护

明确请求：

```text
请只尝试一次通过 Playwright 的 browser_evaluate 读取 document.title，不要改用其他工具，返回策略结果。
```

预期：

- Hook 拒绝，原因包含 `page code execution is disabled`。
- 小鹿不改用 `browser_run_code` 绕过。

### 6.3 文件上传保护

明确请求：

```text
请只尝试一次调用 Playwright 文件上传工具，路径使用 data/security-test/nonexistent.txt，不要创建文件，不要改用其他工具。
```

预期：

- Hook 在浏览器调用前拒绝，原因包含 `file uploads are disabled`。
- 不创建文件，不进行上传，不重复调用。

## 7. 工具与循环检查

检查本轮日志和结果：

- GitHub 工具名只有一份 `mcp__github__*`，没有同名 server 冲突。
- Context7 为 `mcp__context7__resolve-library-id` 和 `mcp__context7__query-docs`。
- Playwright 工具为 `mcp__playwright__*`。
- 每个成功工具结果都追加到上下文，Agent 不重复调用直到达到上限。
- 被 Hook 拒绝的工具只调用一次，拒绝后本轮停止或直接报告。
- Gateway、ConversationCoordinator 和 Delivery 日志没有未处理异常。

## 8. 最终报告

按以下格式返回：

```text
总状态: PASS / FAIL / BLOCKED

1. 启动与注册: PASS
2. 状态命令: PASS
3. GitHub Assistant: PASS
4. Developer Docs: PASS
5. Browser Operator: PASS
6. 无重复 MCP/工具循环: PASS

每项工具调用次数:
- GitHub:
- Context7:
- Playwright:

发现的问题:
- 时间
- 测试项
- 实际行为
- 预期行为
- 关键日志（不得包含 secret）
```

若某项因网络、授权或目标站点变化无法完成，标记 `BLOCKED`，不要反复重试。
