# Issue Agent

一个由 GitHub Issue 驱动的通用 Coding Agent 命令行调度器。支持 Codex、Claude Code、OpenCode，以及任意可从 CLI 调用的 Agent。

## 平台定位

本项目作为 Python CLI 工具运行，支持 Python 3.11+ 可用的 Linux、macOS 和 Windows 环境。
用户在终端中手动启动 `once` 或 `serve`，项目不附带 systemd、launchd 等后台服务配置。

## 工作流

1. 开启 `auto_plan_unlabeled` 后，轮询尚未进入 Agent 工作流（没有 `agent-*` 标签）的新 GitHub Issue；普通业务标签如 `bug`、`enhancement` 不会阻止规划。
2. **Plan-only**：planner agent 只读探索代码库，把 Issue 拆成 1..N 个顺序任务，将 Plan 持久化到
   SQLite 和 `.agent/plan.md`，并评论到 GitHub Issue。orchestrator 会校验 planner 没有产生仓库改动，
   发现改动时立即恢复工作区并按失败处理。此阶段不 commit、不 push、不创建 PR。
3. 人工审核 Plan、补充需求，然后手动添加 `agent-ready`。
4. 根据 `agent:<name>` 标签选择实现 Agent；未指定则使用默认 Agent。
5. 从本地已创建的 Issue worktree 和持久化 Plan 继续执行；直接带 `agent-ready` 发布的 Issue 则在执行前生成 Plan。
6. **逐任务执行**：每个任务写入 `.agent/task.md`。实现 Agent 完成后 orchestrator 独立执行检查，然后
   commit（`feat: <task.title> (#N)`）；默认对最近一个 commit 做**确定性形式审查**（secrets 扫描、
   禁改文件、空 diff，零 LLM 调用），不通过时反馈给实现 Agent，修复后 amend 同一 commit 再审。
   第二次仍不通过时立即停止，不自动进行更多返修；人工检查 review 日志后可重新添加 `agent-ready`
   再次尝试。可用 `[review] task_mode` 切换为 LLM 深度审查（`full`）或关闭（`off`）。
7. **最终阶段**：全部任务通过后，reviewer 对整条分支 diff 做整体功能性 Review（这是唯一的 LLM
   功能审查，覆盖跨任务一致性、正确性、安全、兼容性、迁移与测试）；要求修改时由实现 Agent 修复并
   产生独立 commit（`feat: final review fixes (#N)`）；通过后再执行一次完整 checks。
8. orchestrator 统一 push、创建 PR，并停在 `human-review`。

一个 Issue 对应一个分支、一个 PR（多个顺序 commit）。分支在最终阶段前从不 push，因此 amend/reset 安全。
task 失败时整个 Issue 标记失败并保留已完成任务的分支；重新领取后从第一个未完成任务断点续跑。

它不会自动 merge、部署生产、运行生产迁移或修改 secrets。

## 任务实现流水线

`issue-agent` 是单机调度器：`once` 轮询一轮并等 worker 结束，`serve` 持续轮询。每次调度对每个
可运行的 Issue 领取后走以下流水线。一个 Issue 对应一个分支、一个 PR（多个顺序 commit）。

完整的分支、失败与人工恢复路径见 [开发流程图](docs/development-flow.md)。

### 调度与领取

- `run_once` 拉取带 `agent-ready` 标签的 open Issue（额外包含中断时残留 `agent-running` 的任务）；
  开启 `auto_plan_unlabeled` 时还会拉取没有 `agent-*` 工作流标签的 Issue 进入 plan-only。
- 按 `agent:<name>` 标签选实现 Agent，未指定用 `default_agent`。
- 领取幂等：`pending`/`planned` 总可领；`failed`/`blocked` 只在失败预算内可领；
  `failures >= max_attempts` 后搁置，需人工 `reset`。

### 工作区与 Plan

- 在 `runtime.worktrees/<issue号>` 创建/复用 git worktree，分支 `agent/<N>-<slug>`；
  已有 worktree 目录直接复用，不重复创建。
- 打上 `agent-running`、移除 `agent-ready`。
- 已有持久化 Plan 则复用（断点续跑）；否则 planner agent 把 Issue 拆成 1..N 个顺序任务，
  存 SQLite 与 `.agent/plan.md`。planner 未配置时退化为单任务（整条 Issue 作为唯一任务）。
- 从第一个未完成的任务继续；执行前把工作区硬重置到上一个已完成任务的 commit，
  丢弃半截提交，保证重试干净。

### 逐任务实现与 Review

对 plan 里每个任务（最多返修 2 轮）：

1. 写 `.agent/task.md`，状态 `coding`。
2. 实现 Agent 在 worktree 中执行（prompt 走 stdin）。
3. orchestrator 独立执行 `checks.commands` 验收。
4. 校验确实修改了文件，然后 commit：`feat: <task.title> (#N)`。
5. 按 `[review] task_mode` 对最近一个 commit 做审查（默认 `formal`）：
   - `formal`（默认）：**确定性形式审查**，零 LLM 调用、秒级完成——扫描 diff 中的 secrets
     （AWS/GitHub/OpenAI/Stripe key、私钥块、硬编码密码等）、禁改文件（`.env`、`*.pem`、
     `credentials` 等）与空 commit。不依赖 `reviewer_agent` 配置。
   - `full`：LLM reviewer 只读深度 Review（需要配置 `reviewer_agent`；未配置时跳过）。
     `VERDICT: REQUEST_CHANGES` → 反馈回喂实现 Agent；Reviewer 产生仓库改动 → 立即恢复并按
     失败处理；无合法 verdict → 按普通失败处理。
   - `off`：跳过任务级审查（功能审查仍由最终阶段的整分支 Review 兜底）。
   - 任一模式下不通过 → 实现 Agent 修复并 amend 同一 commit 再审；第二次仍不通过立即停止
     （不再自动返修），等人工检查 review 日志后重试。
6. 通过后该 plan 任务标记 `done`，记录 commit hash。

> 设计取舍：per-task 的功能性深度审查收敛到最终阶段（整分支 Review），任务级只保留确定性
> 形式审查 + checks（测试）兜底，每个任务省去一次 LLM review 调用。若任务 1 存在设计缺陷，
> 后续任务会在其上叠加，最终 Review 拒绝时返工成本最高——因此 plan 的 Acceptance 标准要具体可检。

> 检查是**基线感知**的：agent 开始前，orchestrator 先在锚点提交（`origin/main` 或上一个已完成任务的 commit）逐条运行 `checks.commands`，按命令记录**预存失败**；pytest 失败按 node ID 比较，其他命令仅在退出码和输出均未变化时视为同一个预存失败。之后每次 check 只把"新失败"判为回归并报给 agent 精修。预存失败（例如目标仓库 main 上本就挂掉的测试）被容忍并通过，避免 agent 在不相关的预存错误上反复空耗重试预算——这也是检查报错不再逐轮"漂移"的原因。每个任务失败后其 plan 状态会回退到 `pending`、cursor 复位，可干净断点续跑。

同一 orchestrator 进程中，锚点 commit 与 `checks.commands` 均未变化时会复用基线；自动重试不再
重复运行整套基线检查。锚点或命令配置变化后会重新采集。

### 整分支 Review（最终阶段）

- 全部任务通过后，reviewer 对整条分支 diff 做整体 Review（同样最多 2 轮）。
- 要求修改 → 实现 Agent 修复并产生独立 commit：`feat: final review fixes (#N)`
  （与任务内的 amend 不同）。
- 通过后再完整执行一遍 `checks.commands`，若仍产生改动则再提交。
- 最终修复后 checks 已通过且只读 Reviewer 未改变 HEAD 时，复用该结果，不对同一 commit 连续执行
  两次完整 checks。

### Push 与 PR

orchestrator 统一 push 分支（finalize 前从不 push，因此 amend/reset 安全）、创建 PR、
标记 `human-review` 并评论 PR 地址，流程结束。

### 失败与重试预算

- 可重试错误（命令/检查/Review 要求修改）记 `failed`；意外异常记 `blocked`。
- 每次失败递增该 Issue 的失败计数 `failures`：
  - 预算内：恢复 `agent-ready`，下一轮自动重跑。
  - 预算耗尽：摘掉 `agent-ready` 搁置，需人工 `reset`。
- 重新领取后从第一个未完成任务断点续跑。
- 最终 Review 已产生的修复 commit 和最新反馈也会持久化；最终阶段重试不会退回最后一个普通任务
  commit 后重新返修。

### reset 命令

`issue-agent --config issue-agent.toml reset <issue> [--no-label]` 用于重置搁置的
`failed`/`blocked` 任务：清零失败计数与重试标记、状态回 `pending`（保留 Plan 与已完成的
plan 任务），默认重新添加 `agent-ready`，下一次轮询即重新领取并从断点续跑。只允许重置
`pending`/`planned`/`failed`/`blocked`；运行中或已到 `human-review`/`done` 的任务会被拒绝。

## 快速开始

### 系统依赖

运行需要：

- Python 3.11 或更高版本，包含 `venv` 和 `pip`。
- Git 2.30 或更高版本，并支持 `git worktree`。
- GitHub CLI（`gh`），已对目标仓库完成认证。
- 至少一个可非交互运行的 Coding Agent CLI，例如 Codex、Claude Code 或 OpenCode。
- 目标仓库检查命令所需的构建工具，例如 Node.js、Go、Rust、Java 或数据库客户端。
- 到 GitHub、Agent 服务和项目依赖源的稳定网络连接。

推荐每个 Worker 至少预留 2 个 CPU 核心和 2–4 GB 内存，并为仓库、worktree、依赖缓存及日志预留足够磁盘。最终容量取决于目标项目的编译和测试负载。

安装后先检查：

```bash
python3 --version
git --version
gh --version
gh auth status
codex --version  # 或实际使用的 Agent CLI
```

开发依赖只用于维护本项目；生产安装可使用 `pip install .`，无需安装 `pytest` 和 `ruff`。

```bash
git clone https://github.com/chengcz/coding-agent-orchestrator.git issue-agent
cd issue-agent
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
gh auth login
cp issue-agent.example.toml issue-agent.toml
```

### 初始化 GitHub Labels

没有 `agent-*` 工作流标签的 Issue 可以自动进入 Plan-only；编码阶段仍然完全由 `agent-ready` 控制。首次使用前在目标仓库
一次性创建执行阶段所需标签（重复执行会自动更新已存在的标签，幂等）：

```bash
REPO="chengcz/bioagent"   # 换成编排器实际操作的目标仓库
labels=(
  "agent-ready|0e8a16|Ready for coding-agent implementation"
  "agent-running|1d76db|Implementation in progress"
  "agent-failed|d73a4a|Agent run failed"
  "human-review|fbca04|Awaiting human review"
  "agent:codex|a219d8|Implement with Codex"
  "agent:claude|a219d8|Implement with Claude Code"
  "agent:opencode|a219d8|Implement with OpenCode"
  "agent:claude_opus|a219d8|Implement with Claude Opus"
  "agent:claude_sonnet|a219d8|Implement with Claude Sonnet"
  "reviewer:claude|008672|Review with Claude"
  "reviewer:codex|008672|Review with Codex"
  "resource:database-schema|5319e7|Serialize database schema work"
  "bug|d73a4a|Something isn't working"
  "enhancement|a2eeef|New feature or request"
  "documentation|0075ca|Improvements or additions to documentation"
)
for entry in "${labels[@]}"; do
  name="${entry%%|*}"
  rest="${entry#*|}"
  color="${rest%%|*}"
  desc="${entry#*|*|}"
  gh label create "$name" --repo "$REPO" --color "$color" --description "$desc" 2>/dev/null \
    || gh label edit "$name" --repo "$REPO" --color "$color" --description "$desc" 2>/dev/null \
    || true
done
```

各标签的用途见后文「标签规则」：没有 `agent-*` 标签是自动规划入口；`agent-ready` 是编码执行入口；
`agent:<name>` 选择实现 Agent；
`reviewer:<name>` 按 Issue 选择 Reviewer（当前 `reviewer_agent` 仍是全局配置，需要按 Issue 选择时扩展调度器）；
`resource:database-schema` 对数据库 schema 任务全局串行；`agent-planned`、`agent-running`、`agent-failed`、`human-review`
由编排器维护，不要手工使用。

编辑 `issue-agent.toml`：

CLI 启动时会验证 Agent 名称、并发数、重试次数和 timeout；无效配置会立即报错退出，不会在任务
执行到一半后才进入 `blocked`。

- `runtime.repo`：目标项目的主 checkout，必须已有 `origin`。
- `runtime.worktrees`：每个 Issue 的隔离工作区根目录。
- `github.repo`：目标仓库名，如 `chengcz/bioagent`。
- `runtime.planner_agent`：规划 Agent（把 Issue 拆成多个任务）；不配置时回退为单任务流程。
- `runtime.max_tasks`：单个 plan 的任务数上限，默认 8。
- `runtime.max_attempts`：整个 Issue 的失败重排队预算，默认 3。
- `runtime.max_task_attempts`：未配置 Reviewer 时，单个 task/最终检查在一次 Issue 执行中的内部
  尝试次数，默认 2；与整个 Issue 的预算独立。
- `runtime.fetch_ttl_seconds`：同一仓库 base branch fetch 的短期复用窗口，默认 30 秒；并发 Issue
  共用一次 fetch，避免重复网络请求和 Git 锁竞争。
- `runtime.auto_plan_unlabeled`：是否自动为没有 `agent-*` 工作流标签的新 Issue 生成 Plan；名称为兼容旧配置保留，默认 `false`。
- `runtime.auto_plan_limit`：每轮最多扫描多少个候选 Issue。
- `runtime.log_dir`：每个 Issue 的执行和 Review JSONL 日志目录。
- `checks.commands`：目标项目真实的验收命令。
- `checks.task_commands`：每个 plan task 后执行的快速检查；省略时兼容旧行为并执行全部
  `checks.commands`，设为 `[]` 时只在最终 gate 执行完整检查。
- `checks.timeout_seconds`：每条检查命令的超时，默认 1800 秒；超时会终止整个命令进程组，避免残留
  测试进程持续占用资源；任务取消时也会执行同样的进程树清理。
- `checks.max_workers`：同时运行的 check 批次数，默认跟随 `runtime.max_workers`，防止多 Issue 与
  单 Issue 内并行命令相乘后压垮 CPU/磁盘。
- `checks.baseline_cache_ttl_seconds`：同一 anchor/checks 基线缓存有效期，默认 300 秒；设为 `0` 可
  禁用缓存，适用于强依赖外部环境的集成检查。
- `checks.baseline_cache_max_entries`：baseline LRU 最大条目数，默认 32；相同 key 的并发请求会
  single-flight，只执行一次。
- `review.task_mode`：任务级审查模式，默认 `formal`（确定性形式审查：secrets/禁改文件/空 diff，
  零 LLM 调用）；`full` 恢复每任务 LLM 深度 Review（需配置 `reviewer_agent`）；`off` 跳过任务级
  审查。最终阶段的整分支 LLM Review 不受此项影响。
- 启用已经安装且完成认证的 Agent。

先运行一次：

```bash
issue-agent --config issue-agent.toml once
issue-agent --config issue-agent.toml status
issue-agent --config issue-agent.toml status --active
issue-agent --config issue-agent.toml status --json
issue-agent --config issue-agent.toml report
issue-agent --config issue-agent.toml report --issue 42 --json
```

`status` 输出列：`ISSUE`、`STATUS`、`CURRENT TASK`、`AGENT`、`TOKENS`（输入+输出 token 合计，
k/M 紧凑格式）、`COST`（累计美元开销）、`TIME`（累计 Agent 壁钟耗时）与 `UPDATED`。TOKENS/COST
需要 Agent CLI 输出 JSON envelope（如 Claude CLI 加 `--output-format json`）才有数据，否则仅
TIME 有值；无数据时显示 `-`。`--json` 输出含全部累积字段（`total_input_tokens`、
`total_output_tokens`、`total_cache_read_tokens`、`total_cache_creation_tokens`、
`total_cost_usd`、`total_duration_ms`）。

`report` 同时输出 Issue 的 `queue`/`wall` 以及各 plan task 的累计 `wall`、`agent`、`checks`、
token、cost 和 attempts；
`--json` 还包含逐次 `issue_runs`，适合后续导入监控系统。失败和超时的 Agent 调用也计入统计。

确认无误后，可在当前终端持续轮询；按 `Ctrl+C` 停止：

```bash
issue-agent --config issue-agent.toml serve
```

## Agent 配置

内置的是“配置式适配器”，因此不需要为每个厂商维护一套调度逻辑：

```toml
[agents.codex]
command = "codex exec --json --sandbox workspace-write -"
resume_command = "codex exec resume --json {session_id} -"
max_workers = 2

[agents.claude]
command = "claude -p --output-format json"
review_command = "claude -p --permission-mode plan --output-format json"
resume_command = "claude -p --resume {session_id} --output-format json"
review_resume_command = "claude -p --resume {session_id} --permission-mode plan --output-format json"
max_workers = 1

[agents.custom]
command = "your-agent --prompt {prompt}"
```

命令不含 `{prompt}` 时，prompt 通过 stdin 发送，避免 Issue 太长导致 shell 参数限制；包含占位符时会作为一个参数直接传入，不经过 shell 展开。

### Token 与耗时追踪

orchestrator 对每次 Agent CLI 调用自动记录壁钟耗时，包括失败和超时。它支持 Claude JSON result
envelope（`--output-format json`）与 Codex JSONL（`--json`），自动解析最终消息、输入/输出/缓存/
reasoning token、cost（CLI 提供时）和 session ID。**stdout 会自动 unwrap 为纯文本**，下游解析
（plan JSON、review verdict）不受影响；纯文本 CLI 优雅降级为仅计时。

```toml
[agents.claude]
command = "claude -p --output-format json"
review_command = "claude -p --model sonnet --permission-mode plan --output-format json"
```

用量数据双通道落盘：

- `logs/issue-<N>.jsonl` 的 `agent_call` 事件：每次调用的完整明细（agent、role、duration_ms、
  input_tokens、output_tokens、cache_read_input_tokens、cache_creation_input_tokens、cost_usd）。
- SQLite 状态库：按 Issue 累积 token、成本、Agent/check/wall time，供 `status` 和 `report` 快速查询，
  显示为 TOKENS（输入+输出合计，k/M 紧凑格式）、COST、TIME 三列。
- SQLite 的 `issue_runs`、`agent_calls` 与扩展后的 `plan_tasks` 保存逐次运行、调用以及每个 task 的
  明细；配置 `resume_command` / `review_resume_command` 后，同一 Issue 会复用对应角色的 session。

Issue 标签示例：

- `agent-ready`：允许调度。
- `agent:codex` / `agent:claude`：选择实现 Agent。
- `resource:database-schema`：全局串行，避免 Alembic 多头迁移。
- `agent-planned`、`agent-running`、`agent-failed`、`human-review`：由调度器维护。

### 为不同任务使用不同 Claude 模型

可以为每个 Claude 模型定义一个独立的 Agent 名称。实现命令使用目标模型，`review_command`
使用复审模型：

```toml
[agents.claude_opus]
command = "/Users/chengchaoze/.local/bin/claude -p --model opus --permission-mode acceptEdits"
review_command = "/Users/chengchaoze/.local/bin/claude -p --model sonnet --permission-mode plan"
max_workers = 1
timeout_seconds = 7200

[agents.claude_sonnet]
command = "/Users/chengchaoze/.local/bin/claude -p --model sonnet --permission-mode acceptEdits"
review_command = "/Users/chengchaoze/.local/bin/claude -p --model haiku --permission-mode plan"
max_workers = 1
timeout_seconds = 7200
```

Issue 添加 `agent:claude_opus` 或 `agent:claude_sonnet` 后，编排器会选择相应的实现模型。
`reviewer_agent = "claude_opus"` 则表示所有任务统一使用该 Agent 的 `review_command`，也就是
上例中的 Sonnet Review。当前版本的 `reviewer_agent` 是全局配置，不能仅靠标签让不同 Issue
选择不同 Reviewer。

如果需要按 Issue 选择 Reviewer，可约定 `reviewer:<name>` 标签，例如
`reviewer:claude_sonnet`，并扩展调度器读取该标签；没有标签时回退到全局
`reviewer_agent`。Review 命令应使用只读的 `--permission-mode plan`，避免 Reviewer 修改工作区。

### 任务级审查模式

```toml
[review]
task_mode = "formal"  # formal（默认）| full | off
```

- `formal`：确定性形式审查（secrets/禁改文件/空 diff），零 LLM 调用，不依赖 `reviewer_agent`。
- `full`：每个任务 commit 后由 `reviewer_agent` 做 LLM 深度 Review（旧默认行为）。
- `off`：跳过任务级审查。

无论哪种模式，最终阶段的整分支 LLM Review（需配置 `reviewer_agent`）始终执行，是唯一的
功能性深度审查。`full` 模式未配置 `reviewer_agent` 时任务级审查跳过；`formal` 模式审查
不通过同样占用 2 轮返修预算，第二次仍不通过则搁置等人工处理。

## 目标项目约定

目标项目应包含 `AGENTS.md`，Claude 项目可增加一个很短的 `CLAUDE.md`，只引用 `AGENTS.md`，避免规则分叉。建议把目标项目的检查统一为 `scripts/check.sh`，然后配置：

```toml
[checks]
commands = ["./scripts/check.sh"]
```

`.agent/plan.md`、`.agent/task.md`、feedback 和 check output 由 orchestrator 创建。Workspace 操作会
显式从 status、commit/amend 和 clean 中排除 `.agent`，因此它们不会再造成“有代码改动”的误判或进入 PR。

## BioAgent 任务发布

本目录保存 `chengcz/bioagent` 的本地编排运行数据与任务发布说明。运行数据位于
`repo/`、`worktrees/`、`state/` 和 `logs/`，这些目录不会提交到本仓库。

## 发布流程

1. 在 `chengcz/bioagent` 创建 GitHub Issue。
2. 按本文后面的“GitHub Issue 模板”填写完整任务，确保范围可由一个 PR 完成。
3. 选择实现 Agent：添加 `agent:codex` 或 `agent:claude` 标签；不添加时使用默认 Codex。
4. 确认依赖任务已完成、验收标准可执行后，再添加 `agent-ready` 标签。
5. 前台启动编排器：

   ```bash
   .venv/bin/issue-agent --config bioagent.toml --verbose serve
   ```

6. planner 先把 Issue 拆成多个任务，实现 Agent 逐个完成：每个任务独立检查、commit 与形式审查
   （默认确定性审查，见 `[review] task_mode`）；全部任务通过后做整分支 LLM Review 和最终检查，
   编排器再统一推送并创建 PR，同时把 Issue 标记为
   `human-review`。必须由人审查和合并；编排器不会自动合并或部署。

命令行发布示例：

```bash
gh issue create \
  --repo chengcz/bioagent \
  --title "[V1] Implement real volcano plot execution" \
  --body "请按 README 中的 GitHub Issue 模板填写完整任务" \
  --label enhancement \
  --label agent:claude \
  --label agent-ready
```

也可以先创建草稿 Issue，补充清楚后再发布：

```bash
gh issue edit ISSUE_NUMBER \
  --repo chengcz/bioagent \
  --add-label agent:codex \
  --add-label agent-ready
```

## 内容要求

每个任务必须包含：

- 背景与问题：说明当前行为、用户影响和可复现证据。
- 目标：一句话描述完成后的可观察结果。
- 范围：指出允许修改的模块、接口和数据结构。
- 非目标：明确本任务不做的工作，防止范围蔓延。
- 实现约束：兼容性、安全、迁移、权限和依赖限制。
- 验收标准：使用可验证的清单，避免“优化一下”“完善功能”等模糊描述。
- 测试要求：列出必须运行或新增的测试；需要外部服务时说明替代验证方式。
- 依赖与风险：关联前置 Issue、资源锁和人工决策。

任务不得要求编码 Agent 执行 push、创建或合并 PR、部署、修改 secrets、运行生产迁移或生产操作；
这些操作必须留在编排器或人工流程中。

## 标签规则

- `agent-ready`：任务内容完整且允许领取；这是唯一的调度入口。
- `agent:codex`：由 Codex 实现。
- `agent:claude`：由 Claude Code 实现。
- `resource:database-schema`：涉及数据库 schema 时添加，同一进程内串行执行。
- `agent-planned`、`agent-running`、`agent-failed`、`human-review`：由编排器维护，不要手工用于发布任务。
- `bug`、`enhancement`、`documentation`：描述任务类型，可与 Agent 标签组合。

不要同时添加多个 `agent:<name>` 标签。一个 Issue 应对应一个可独立审查的 PR；大型需求应拆成有依赖关系的多个 Issue。

## Agent 与 Review

默认实现 Agent 是 Codex。Issue 使用 `agent:claude` 时由 Claude Code 实现。任务级审查默认是
确定性形式审查（`[review] task_mode = "formal"`，见上文）；LLM 深度 Review 收敛到最终阶段，
当前配置由 Codex 做只读 Review，因此 Claude 实现的任务在整分支 Review 时仍会得到跨 Agent 复审。
Review 未明确给出批准结论时，任务会被标记为失败并等待人工处理。

`runtime.planner_agent` 指定负责拆解 Issue 的规划 Agent（通常复用 reviewer 的只读命令，没有则用实现命令）；
`runtime.max_tasks` 限制单个 plan 的任务数上限，防止 planner 失控。planner 输出解析失败或超过上限时，
任务被标记为失败并可重领后重新规划。每个任务独立 commit 与形式审查（默认确定性审查），任务之间的修复
amend 同一个 commit；最终阶段针对整条分支做 LLM Review，修复产生独立 commit。

### Issue 执行与 Review 日志

`runtime.log_dir` 下会按 Issue 写入两份 JSONL 文件：

```text
issue-42.jsonl          # 规划、编码尝试、检查、commit、返修、push、PR、agent_call 用量与失败状态
issue-42.reviews.jsonl  # 每次 task review（含形式审查）和 final review 的完整输出及 verdict
```

执行日志会记录当前 plan 子任务、尝试次数、检查命令、错误和最终 PR 地址；每次 Agent CLI 调用
还会写入 `agent_call` 事件（agent、role、duration_ms，以及 CLI 输出 JSON envelope 时的
input/output/cache token 与 cost 明细）。review 日志会保留
`REQUEST_CHANGES` 的具体反馈，方便分析是否因 Issue 描述、计划、实现或测试不足而返修。日志可能包含
Issue 内容和 Agent 输出，应按目标仓库的访问控制保护 `log_dir`，不要写入公开目录。

### GitHub Issue 模板

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

也可以直接运行「快速开始」里的「初始化 GitHub Labels」命令，一次性创建编排器用到的全部标签。

发布前检查：

- [ ] Issue 可以由一个独立 PR 完成。
- [ ] 已审核 Issue Agent 发布的 Plan，或已写明足够明确的验收标准和测试要求。
- [ ] 前置 Issue 已完成；否则暂时不要添加 `agent-ready`。
- [ ] 没有同时添加 `agent:codex` 和 `agent:claude`。
- [ ] 没有手工添加 `agent-running`、`agent-failed` 或 `human-review`；这些标签由编排器维护。

需要暂停尚未领取的任务时，从 Issue 右侧 **Labels** 中移除 `agent-ready`。任务已出现
`agent-running` 后不要靠修改标签强行停止，应先安全停止前台编排器并检查任务状态。

## 手动运行与维护

当前版本只提供手动启动的命令行程序，不安装或管理后台服务。一个仓库对应一个 orchestrator
进程；需要持续轮询时，在终端中运行 `serve` 并保持该终端会话。

### 1. 目录规划

每个仓库必须拥有独立的主 checkout、worktree 根目录、SQLite 文件和配置文件：

```text
/opt/issue-agent/                     程序与虚拟环境
/etc/issue-agent/                             配置和非敏感环境文件
/srv/issue-agent/repo-a/repo/                 repo-a 主 checkout
/srv/issue-agent/repo-a/worktrees/            repo-a 工作区
/var/lib/issue-agent/repo-a/state.sqlite3     repo-a 状态
/srv/issue-agent/repo-b/repo/                 repo-b 主 checkout
/srv/issue-agent/repo-b/worktrees/            repo-b 工作区
/var/lib/issue-agent/repo-b/state.sqlite3     repo-b 状态
```

禁止两个实例共享同一 SQLite 文件、worktree 根目录或目标仓库。不同仓库如果会修改同一数据库 schema、共享生成文件或占用独占测试设备，需要在 orchestrator 之外提供机器级锁；当前 `resource:database-schema` 只在单个进程内生效。

### 2. 系统依赖

- Python 3.11+，包含 `venv`、`pip` 和 SQLite 支持。
- Git 2.30+，以及对目标仓库的 fetch/push 权限。
- GitHub CLI，并完成目标仓库认证。
- 至少一个支持非交互调用的 Agent CLI。
- 目标仓库自身的编译、测试和格式检查工具。

Debian/Ubuntu 的基础包示例：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git ca-certificates
```

macOS 可通过 Homebrew 安装基础工具：

```bash
brew install python git gh
```

安装 Agent CLI 和 GitHub CLI 时应遵循各自官方说明。使用启动 `issue-agent` 的同一个 OS 用户完成认证，
并验证非交互命令在该用户环境中可用。

### 3. 安装 CLI

Linux 推荐创建专用账户：

```bash
sudo useradd --create-home --shell /bin/bash agent
sudo install -d -o agent -g agent /opt/issue-agent /srv/issue-agent /var/lib/issue-agent
sudo install -d -o root -g agent -m 0750 /etc/issue-agent
```

以该账户克隆和安装：

```bash
sudo -u agent git clone https://github.com/chengcz/coding-agent-orchestrator.git /opt/issue-agent
sudo -u agent python3 -m venv /opt/issue-agent/.venv
sudo -u agent /opt/issue-agent/.venv/bin/pip install /opt/issue-agent
```

如果这台机器也用于维护和验证 Issue Agent 本身，可改为安装 `/opt/issue-agent[dev]`。

目标仓库也由该账户克隆，且必须配置 `origin`：

```bash
sudo -u agent git clone git@github.com:OWNER/repo-a.git /srv/issue-agent/repo-a/repo
```

### 4. 每仓库配置

将示例配置复制为 `/etc/issue-agent/repo-a.toml`，至少修改：

```toml
[runtime]
repo = "/srv/issue-agent/repo-a/repo"
worktrees = "/srv/issue-agent/repo-a/worktrees"
state_db = "/var/lib/issue-agent/repo-a/state.sqlite3"
poll_seconds = 60
max_workers = 2
max_attempts = 3
default_agent = "codex"
planner_agent = "claude"
max_tasks = 8

[github]
repo = "OWNER/repo-a"
base_branch = "main"
ready_label = "agent-ready"

[checks]
commands = ["./scripts/check.sh"]

[agents.codex]
command = "codex exec --sandbox workspace-write -"
max_workers = 2
timeout_seconds = 7200
```

保护配置并创建状态目录：

```bash
sudo install -d -o agent -g agent /var/lib/issue-agent/repo-a /srv/issue-agent/repo-a/worktrees
sudo chown root:agent /etc/issue-agent/repo-a.toml
sudo chmod 0640 /etc/issue-agent/repo-a.toml
```

先以单次模式验收，确认 GitHub 和 Agent 登录态、仓库权限及检查命令均正常：

```bash
sudo -u agent /opt/issue-agent/.venv/bin/issue-agent --config /etc/issue-agent/repo-a.toml once
sudo -u agent /opt/issue-agent/.venv/bin/issue-agent --config /etc/issue-agent/repo-a.toml status
```

### 5. 手动启动

处理一轮任务并等待本轮 Worker 完成：

```bash
issue-agent --config /etc/issue-agent/repo-a.toml once
```

在当前终端持续轮询，按 `Ctrl+C` 停止：

```bash
issue-agent --config /etc/issue-agent/repo-a.toml --verbose serve
```

查看持久化状态：

```bash
issue-agent --config /etc/issue-agent/repo-a.toml status
issue-agent --config /etc/issue-agent/repo-a.toml status --active
issue-agent --config /etc/issue-agent/repo-a.toml status --json
```

默认输出便于人工查看的任务表，包含 Issue、整体状态、当前 plan 子任务、Agent 和最近更新时间。
`--active` 只显示正在调度或执行的任务；`--json` 输出适合脚本处理的完整状态字段。

### 6. 多 Worker 与容量

`runtime.max_workers` 限制单仓库同时处理的 Issue 数，`agents.<name>.max_workers` 只限制该 Agent
CLI 的同时调用数；checks、Git 和 GitHub 操作不会继续占用 Agent 配额。一台机器上所有实例的
Worker 总数还应受 CPU、内存、磁盘 I/O 和 API 限额约束。
Planner 和 Reviewer 使用与其 Agent 名称对应的同一配额；当实现 Agent 与 Reviewer 不同时，不会绕过
Reviewer 的 `max_workers`。

建议从每仓库 1 个 Worker 开始，观察完整编译和测试的峰值资源后再增加。一般可按每个 Worker 至少 2 个 CPU 核心、2–4 GB 内存估算；大型项目应以实测为准。确保磁盘容量可以容纳主 checkout、并行 worktrees、构建产物和依赖缓存。

### 7. GitHub 与凭据

GitHub 凭据只授予目标仓库所需的 Issues、Contents 和 Pull requests 权限。不要把生产密钥、部署凭据或数据库管理员凭据放入运行环境。通过以下命令验证身份：

```bash
sudo -u agent gh auth status
sudo -u agent git -C /srv/issue-agent/repo-a/repo fetch origin main
```

如需通过环境变量提供凭据，将其放在 root 可读的凭据管理位置。`/etc/issue-agent/environment` 和 `%i.env` 只适合由权限严格控制的环境文件，不应提交到 Git。

### 8. 升级与回滚

升级前按 `Ctrl+C` 停止正在运行的 `serve`，避免在 Worker 执行期间替换代码：

```bash
sudo -u agent git -C /opt/issue-agent pull --ff-only
sudo -u agent /opt/issue-agent/.venv/bin/pip install /opt/issue-agent
```

安装了 `[dev]` 依赖时，上线前运行项目自身检查：

```bash
cd /opt/issue-agent
.venv/bin/ruff check .
.venv/bin/pytest -q
```

回滚时检出已验证的 Git 提交并重新安装包，然后手动启动 CLI。不要删除 SQLite 或 worktree 来代替状态恢复。

### 9. 备份与恢复

备份范围包括每个仓库的 SQLite 状态文件、配置文件和必要的 Agent CLI 认证资料。仓库代码可从 GitHub 恢复，worktree 通常无需备份。复制 SQLite 前应停止对应实例，或使用 SQLite 在线备份工具生成一致快照。

进程重启时，调度器会把中断状态标记为可重试，并重新读取仍带 `agent-running` 标签的 Issue。恢复后检查：

```bash
issue-agent --config /etc/issue-agent/repo-a.toml status
git -C /srv/issue-agent/repo-a/repo worktree list
gh issue list --repo OWNER/repo-a --label agent-running
```

### 10. 故障排查

- CLI 启动失败：添加 `--verbose` 查看日志，并检查配置路径和 Python 环境。
- 无法领取 Issue：确认标签名称、GitHub 仓库名和 `gh auth status`。
- 无法创建 worktree：检查 `origin/<base_branch>`、目录权限和残留 worktree。
- Agent 命令找不到：确认当前终端的 PATH，或在 TOML 中使用绝对路径。
- push/PR 失败：验证服务账户的 SSH key 或 GitHub token，以及仓库分支策略。
- 任务长期占用：检查 Agent 超时、目标项目测试超时及机器资源；不要直接启动第二个同仓库实例绕过阻塞。
- 任务停在 `failed`/`blocked` 且不再被领取：失败预算（`failures >= max_attempts`）耗尽，任务被搁置，需用 `reset` 命令重置。

当前版本不支持多机调度、跨实例锁、自动合并或生产部署。需要高可用多机运行时，应先引入集中式状态库、租约和分布式锁设计。

### 11. 重置失败/阻塞任务

`failed` 或 `blocked` 的任务在重试预算耗尽后会被搁置（不再自动恢复 `agent-ready`），需要人工重置后才能再次运行：

```bash
issue-agent --config issue-agent.toml reset 42            # 重置状态并重新入队
issue-agent --config issue-agent.toml reset 42 --no-label # 只重置本地状态，稍后手动加 agent-ready
```

- 重置会清空该 Issue 的失败计数（`failures`）和重试标记，把状态改回 `pending`，保留已存在的 Plan 并从第一个未完成任务断点续跑；已完成（`done`）的 plan 项不会被清掉。
- 默认会重新添加 `agent-ready` 标签（并移除 `agent-failed`/`agent-running`），下一次轮询即重新领取；`--no-label` 跳过标签操作，适合需要先修改 Issue 描述或 Plan 再放行的情况。
- 只允许重置 `pending`/`planned`/`failed`/`blocked` 状态；运行中或已进入 `human-review`/`done` 的任务会被拒绝，避免干扰进行中的 worker 或重复创建 PR。


## 安全边界

- 为 GitHub token 只授予目标仓库的 Issues/Contents/Pull requests 所需权限。
- Agent 在 worktree 中运行；Codex 示例使用 `workspace-write` sandbox。
- Agent 不掌握 GitHub 操作流程；push/PR 由 orchestrator 控制。
- 不自动 merge main，不自动部署，不接触生产数据库和 secrets。
- 推荐专用 OS 用户运行；不要把生产凭据传给 worker。
- SQLite 保存任务状态，常驻进程重启后不会重复领取已经处于运行或人审状态的任务。

## 当前边界与下一阶段

MVP 使用 `gh` CLI 和 SQLite，适合单机 1–5 个并发 worker。多机部署时再替换为 GitHub App + PostgreSQL/Redis 分布式锁，并增加心跳、取消、PR 创建后的 review 循环和指标监控。不要在单机版上直接启动多个 orchestrator 实例。

Codex 的非交互自动化入口是 `codex exec`；官方也建议非交互运行使用 workspace-write sandbox。GitHub Actions 中可另行使用官方 Codex Action，但它不是本地常驻调度器的必需依赖。
