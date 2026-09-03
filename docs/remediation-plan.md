# Issue Agent 可观测性与性能修复计划

更新时间：2026-09-04

## 目标

- 准确报告每个 Issue、plan task、attempt 和 Agent 调用的耗时、token、缓存 token、成本与结果。
- 支持 Claude 单 JSON envelope 和 Codex JSONL，不因结构化输出破坏 plan/review 文本解析。
- 减少重复 checks、轮询等待、baseline 重复计算和资源锁导致的队头阻塞。
- 保持 provider-neutral 调度边界、worktree 隔离、人工 PR 审核和既有恢复语义。

## 实施清单

- [x] 为命令失败和超时保留调用耗时及可解析输出。
- [x] 增加 Codex JSONL 最终消息、input/output/cached/reasoning token 解析。
- [x] 新增 Issue run、Agent call、task 累计指标的数据表和迁移逻辑。
- [x] 记录 Issue queue/wall time、task wall time和 checks wall time，并在重启时关闭 interrupted run。
- [x] 新增 `issue-agent report [--issue N] [--json]` 报告入口。
- [x] checks 增加独立并发额度和可选 `task_commands`。
- [x] baseline 缓存增加并发 single-flight、TTL 清理和容量上限。
- [x] 无代码改动时在运行完整 task checks 前快速失败。
- [x] 数据库资源锁在全局 worker 槽之前获取，避免等待锁时占用执行槽。
- [x] worker 完成后唤醒调度器，并发获取 ready/running Issue。
- [x] 修复自定义 `ready_label` 在失败重排队和 reset 中被忽略的问题。
- [x] plan-only Issue 使用 `agent-planned` 标记，并允许失败后复用已持久化 plan。
- [x] `.agent` 运行文件从 status、commit/amend 和 clean 中隔离。
- [x] 完成可配置 Agent session resume 的编排接线与测试。
- [x] 补齐 Codex JSONL、失败/超时计量、task/Issue 报告、缓存并发、调度和标签测试。
- [x] 更新示例配置、README 与开发流程文档。
- [x] 运行 `ruff check .` 和 `pytest -q`，修复全部回归（168 passed）。

## 报告口径

- `wall`：一次 Issue/task 执行从进入处理到退出的端到端时间。
- `agent`：Agent CLI 调用 wall time 之和，包含成功、失败和超时。
- `checks`：baseline、task checks、final checks 实际等待时间之和。
- `tokens`：input + output；cached input 和 reasoning output 单独保存。
- Issue 与 task 行保存生命周期累计值；`issue_runs` 和 `agent_calls` 保存逐次明细。

## 性能验收

- 同一 anchor/check 配置的并发 baseline 只执行一次。
- baseline 缓存不会随长期运行无限增长。
- 配置 `task_commands = []` 时，中间 task 不跑完整检查，但 final checks 仍强制执行。
- `resource:database-schema` 任务等待资源锁时不占全局 worker semaphore。
- worker 完成或重排队后无需等待完整 `poll_seconds` 才能再次调度。
- 自定义 `github.ready_label` 在领取、失败重排队、plan 提示和 reset 中保持一致。
- `.agent` 文件不能让“无代码改动”误判为有改动，也不会进入 task commit。
