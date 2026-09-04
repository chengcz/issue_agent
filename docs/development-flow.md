# Issue Agent 开发流程图

本文档描述当前单机 orchestrator 从 GitHub Issue 领取到 Pull Request 等待人工审核的完整流程。
实现细节和运维说明仍以 [README](../README.md) 为准。

## 主流程

```mermaid
flowchart TD
    Start[once / serve 启动] --> Recover[恢复被中断的 SQLite 状态]
    Recover --> Poll[拉取 agent-ready、agent-running<br/>以及可选的无 agent-* Issue]

    Poll --> Kind{Issue 类型}
    Kind -->|无 agent-* 标签| PlanClaim[幂等领取 Plan-only]
    PlanClaim --> PlanWorkspace[创建或复用 worktree<br/>重置到 origin/base]
    PlanWorkspace --> Planner[只读 Planner 生成 1..N 个任务]
    Planner --> PlanGuard{工作区被修改?}
    PlanGuard -->|是| PlanRestore[恢复 HEAD 并记录失败]
    PlanGuard -->|否| SavePlan[Plan 写入 SQLite 和 .agent/plan.md]
    SavePlan --> PlanComment[评论 Plan，添加 agent-planned<br/>等待人工审核]
    PlanComment --> WaitReady[人工添加 agent-ready]

    Kind -->|agent-ready / agent-running| Claim[按 agent 标签路由并幂等领取]
    WaitReady --> Claim
    Claim --> Workspace[创建或复用 Issue worktree]
    Workspace --> PersistPlanning[持久化 planning 状态]
    PersistPlanning --> RunningLabel[添加 agent-running<br/>移除 agent-ready]
    RunningLabel --> PlanSource{已有持久化 Plan?}
    PlanSource -->|否| GeneratePlan[生成并持久化 Plan]
    PlanSource -->|是| Resume[定位第一个未完成任务]
    GeneratePlan --> Resume
    Resume --> Anchor[硬重置到上一个完成任务的 commit<br/>或 origin/base]
    Anchor --> Baseline[逐命令采集 checks 基线]
    Baseline --> TaskLoop[进入顺序任务循环]

    TaskLoop --> Coding[写 .agent/task.md<br/>状态 coding，执行实现 Agent]
    Coding --> Checks[orchestrator 独立执行 checks]
    Checks -->|新增失败| TaskRetry{任务内还有尝试?}
    TaskRetry -->|是| Coding
    TaskRetry -->|否| Failed
    Checks -->|通过或仅预存失败| Changed{存在代码改动?}
    Changed -->|否| TaskRetry
    Changed -->|是| Commit[首次 commit；返修时 amend]
    Commit --> ReviewMode{review.task_mode}
    ReviewMode -->|off| TaskDone[任务标记 done 并记录 commit]
    ReviewMode -->|formal 默认| FormalReview[确定性形式审查<br/>secrets / 禁改文件 / 空 diff]
    ReviewMode -->|full| FullGate{配置 Reviewer?}
    FullGate -->|否| TaskDone
    FullGate -->|是| TaskReview[只读 LLM Review 最近一个 commit]
    FormalReview --> FormalGuard{形式审查通过?}
    FormalGuard -->|否，首次| Coding
    FormalGuard -->|否，第二次| ManualReview[停止自动返修，等待人工处理]
    FormalGuard -->|是| TaskDone
    TaskReview --> ReviewGuard{Review 结果}
    ReviewGuard -->|工作区被修改| RestoreReview[恢复到 Review 前 HEAD]
    RestoreReview --> Failed
    ReviewGuard -->|无合法 verdict| Failed
    ReviewGuard -->|REQUEST_CHANGES，首次| Coding
    ReviewGuard -->|REQUEST_CHANGES，第二次| ManualReview
    ReviewGuard -->|APPROVE| TaskDone
    TaskDone --> MoreTasks{还有未完成任务?}
    MoreTasks -->|是| TaskLoop
    MoreTasks -->|否| FinalReviewer{配置 Reviewer?}
    FinalReviewer -->|是| FinalReview[整分支只读 Review]
    FinalReviewer -->|否| FinalChecks

    FinalReview --> FinalResult{最终 Review 结果}
    FinalResult -->|REQUEST_CHANGES，首次| FinalFix[实现 Agent 修复、checks、独立 commit]
    FinalFix --> FinalReview
    FinalResult -->|REQUEST_CHANGES，第二次| ManualReview
    FinalResult -->|无合法 verdict / 只读违规| Failed
    FinalResult -->|APPROVE| FinalChecks[完整执行最终 checks]
    FinalChecks -->|失败| FinalFix
    FinalChecks -->|通过| PushState[持久化 pushing 状态]
    PushState --> Push[push 分支并创建 PR]
    Push --> HumanState[持久化 human_review]
    HumanState --> HumanLabel[添加 human-review、评论 PR 地址]

    PlanRestore --> PlanFailureBudget{失败预算耗尽?}
    Failed --> FailureBudget{失败预算耗尽?}
    FailureBudget -->|否| Requeue[添加 agent-failed 和 agent-ready]
    FailureBudget -->|是| Park[添加 agent-failed，等待 reset]
    PlanFailureBudget -->|否| Poll
    PlanFailureBudget -->|是| PlanPark[保持无 agent-* 标签<br/>拒绝再次领取，等待 reset]
    ManualReview --> Park
    Requeue --> Poll
    Park --> Reset[人工 issue-agent reset]
    PlanPark --> Reset
    Reset --> Poll
```

## 持久化顺序原则

- 领取、规划、编码、测试、Review、push 和人审状态均先写入 SQLite，再执行对应的 GitHub Label
  变化；进程重启后依靠 SQLite 与残留的 `agent-running` 标签恢复。
- 每个完成的 Plan 任务保存 commit hash。失败重试先回到最近完成任务的 commit，并将该任务最后一次
  错误重新提供给实现 Agent。
- push 和 PR 创建只由 orchestrator 执行；编码 Agent、Planner 与 Reviewer 的 prompt 均禁止执行
  GitHub、merge、部署、迁移、secrets 等外部操作。

## 只读与检查边界

- Planner 开始前工作区会回到 `origin/<base_branch>`；Planner 或 Reviewer 如果产生 tracked 或
  non-ignored untracked 改动，orchestrator 会恢复到安全 commit 并将本轮标记失败。
- 任务级审查按 `review.task_mode` 分档：`formal`（默认）为确定性检查（diff 中 secrets 模式、
  禁改文件、空 commit），零 LLM 调用且不依赖 Reviewer 配置；`full` 为 LLM 只读深度 Review；
  `off` 跳过。git 基础设施故障（如 worktree 损坏）以可重试错误处理，参与任务内返修循环。
- checks 基线按命令隔离。pytest 使用失败 node ID 判断新增回归；其他命令仅在退出码和合并后的输出均与
  基线一致时容忍。
- 明确的第二次不通过（LLM `REQUEST_CHANGES` 或形式审查拒绝）停止自动返修；无合法 verdict 和只读
  违规属于普通失败，在 Issue 失败预算内重新排队。
- 每次 Agent CLI 调用的耗时与 token 用量（CLI 输出支持的结构化格式时）双写：`agent_call` 事件进
  JSONL 执行日志，按 Issue/task 累积总量进 SQLite；Codex JSONL 和 Claude JSON envelope 均可解析，
  失败/超时调用也保留耗时。`report` 分别显示 wall/agent/check time 与 token/cost。
- 相同 anchor/checks 的并发 baseline 使用 single-flight 和有界 LRU；`checks.task_commands` 可将中间
  task 限制为快速检查，最终 gate 始终执行完整 `checks.commands`。
