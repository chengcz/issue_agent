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

- 前置 Issue/PR：无（有前置任务时见下方「依赖与 blocked-by」）
- 资源锁标签：无；涉及数据库 schema 时使用 `resource:database-schema`
- 已知风险或待人工决策：无

---

## 依赖与 blocked-by

有前置任务时**必须**在 GitHub 上建立原生 blocked-by 关系，二选一：

- 网页端：打开本 Issue，右侧 **Relationships → Blocked by**，选择前置 Issue；
- 命令行：`gh issue edit <本 Issue> --add-blocked-by <前置 Issue>`。

前置 Issue 未关闭时本 Issue 不会被领取，被拦住的当轮评论一次「等待依赖」，依赖全部关闭后再评论
一次「依赖已关闭」，期间不需要人工反复增删 `agent-ready`。只有原生关系会真正拦住领取；正文里
写 `Depends on #12` 只作说明，与原生不一致时编排器会评论提醒（以原生为准），写成了自依赖也会被
拦住并提示。已在进行中的任务不受新 blocker 影响，避免崩溃恢复被中途挡住。

## 正文自带计划与 auto-ready

如果 Issue 正文本身就是一份完整实现计划（有序、可执行、带验收标准），且该 Issue 不是拆分出来的
子 Issue，那么规划阶段会直接采用它、评论 Plan 并自动添加 `agent-ready`，跳过人工审核 Plan 这一步，
避免对同一份内容做第二次规划产生漂移。不需要这个行为时把 `auto_ready_with_plan` 设为 `false`。

## 澄清提问与 agent-needs-info

任务描述不足以拆出具体任务时，Planner 会直接在 Issue 里提问并添加 `agent-needs-info`，Issue 回到
待领取状态但不消耗失败预算。人工在评论里回答后，下一轮轮询自动移除该标签并带着回答重新规划，
不需要手动增删 `agent-ready`。追问轮数有上限；用尽后 `agent-needs-info` 会保留，评论会指示人工
处理后执行 `issue-agent reset`。

## 拆分与子 Issue

任务过大时 Planner 可以拆分成多个子 Issue：编排器创建子 Issue 并建立 parent 与兄弟 blocked-by
关系，子 Issue 继承父 Issue 的非 `agent-*` 标签，但**不会**自动添加 `agent-ready`——需要人工逐个
判断后放行。父 Issue 停在 `split` + `human-review`，既不自动关闭也不自动重新规划；同意拆分就不必
再动父 Issue，不同意则对父 Issue 执行 `issue-agent reset`（会清掉拆分记录，父 Issue 按单 Issue
重新规划；已创建的子 Issue 不会自动关闭，需要人工处理）。拆分中途失败时父 Issue 留在重试路径，
失败评论会列出已创建的子 Issue，重试只补建缺失的部分。

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
- [ ] 前置 Issue 已完成；否则用原生 **Blocked by** 声明依赖，而不是靠不加 `agent-ready` 来回避。
- [ ] 没有同时添加 `agent:codex` 和 `agent:claude`。
- [ ] 没有手工添加 `agent-running`、`agent-failed`、`human-review` 或 `agent-needs-info`；这些标签由编排器维护。

需要暂停尚未领取的任务时，从 Issue 右侧 **Labels** 中移除 `agent-ready`。任务已出现
`agent-running` 后不要靠修改标签强行停止，应先安全停止前台编排器并检查任务状态。