# GitHub Issue 模板

为 Agent 创建编码任务时按本模板填写完整 Issue，确保范围可由一个 PR 完成。内容要求见
[README「内容要求」](../README.md#内容要求)。

## 背景与问题

说明当前行为、用户影响、复现步骤或相关代码位置。

## 目标

用一句话描述完成后可观察、可验证的结果。

## 范围

- 必须实现的行为：
- 允许修改的模块/API/数据结构：
- 需要保持兼容的行为：

## 非目标

- 本 Issue 明确不处理：

## 实现约束

- 安全与权限边界：
- 向后兼容要求：
- 数据迁移或回滚要求：
- 不得 push、创建/合并 PR、部署、修改 secrets 或操作生产环境。

## 验收标准

- [ ] 给定……时，系统应……
- [ ] 错误输入或失败路径应……
- [ ] 现有兼容行为应……
- [ ] 文档或 API 契约已同步更新。

## 测试要求

- [ ] 新增或更新针对本行为的自动化测试。
- [ ] 运行与改动范围对应的单元/集成测试。
- [ ] 记录无法自动化验证的项目和人工验证步骤。

## 依赖与风险

- 前置 Issue/PR：无
- 资源锁标签：无；涉及数据库 schema 时使用 `resource:database-schema`
- 已知风险或待人工决策：无

---

## 在 GitHub 网页发布给 Agent

新建 Issue 后可以先不添加任何 Label，让 Issue Agent 只生成 Plan：

1. 打开 `chengcz/bioagent` 仓库的 **Issues** 页面，点击 **New issue**。
2. 填写标题和本模板中的所有必填章节，然后点击 **Create** 或 **Submit new issue**。
3. 不要添加 `agent-ready`、`agent-running` 或 `agent-failed` 等 `agent-*` 工作流标签。可保留 `bug`、`enhancement` 等业务标签；下一轮轮询会发布 Plan 评论，但不会修改代码或创建 PR。
4. 人工审核 Plan；需要时编辑 Issue 补充需求。
5. 准备执行时，在 Issue 右侧找到 **Labels**，选择且只选择一个实现 Agent：
   - `agent:codex`：由 Codex 实现；未选择 Agent 标签时也默认使用 Codex。
   - `agent:claude`：由 Claude Code 实现。
6. 最后添加 `agent-ready`。标签保存后，前台编排器会在下一次轮询时使用已审核的 Plan 开始编码。

创建 Issue 时已添加 `bug`、`enhancement` 等普通 Label 仍会自动进入 Plan-only；只有 `agent-*` 工作流标签才会阻止该阶段。

如果列表中没有 `agent-ready`：

1. 打开仓库的 **Issues** 页面。
2. 点击页面上方的 **Labels**。
3. 点击 **New label**。
4. Name 填写 `agent-ready`，Description 可填写 `Ready for coding-agent implementation`，颜色可使用 `0e8a16`。
5. 点击 **Create label**，返回 Issue 后按上述步骤添加该标签。

也可以直接运行 README「快速开始」中的「初始化 GitHub Labels」命令，一次性创建编排器用到的全部标签。

发布前检查：

- [ ] Issue 可以由一个独立 PR 完成。
- [ ] 已审核 Issue Agent 发布的 Plan，或已写明足够明确的验收标准和测试要求。
- [ ] 前置 Issue 已完成；否则暂时不要添加 `agent-ready`。
- [ ] 没有同时添加 `agent:codex` 和 `agent:claude`。
- [ ] 没有手工添加 `agent-running`、`agent-failed` 或 `human-review`；这些标签由编排器维护。

需要暂停尚未领取的任务时，从 Issue 右侧 **Labels** 中移除 `agent-ready`。任务已出现
`agent-running` 后不要靠修改标签强行停止，应先安全停止前台编排器并检查任务状态。