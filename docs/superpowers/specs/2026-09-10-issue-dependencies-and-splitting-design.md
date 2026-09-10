# Issue 依赖门禁、自动放行、澄清回环与拆分 — 设计

日期：2026-09-10
状态：待实现

## 背景与动机

当前编排器对 Issue 之间没有任何结构感知：

- **依赖只能靠人记**。`docs/issue-template.md` 有 `## 依赖与风险 / 前置 Issue/PR：无` 一节，但那是纯
  人类可读的说明，编排器不看。人忘了确认前置 Issue 就加 `agent-ready`，编码会在残缺的代码基础上进行。
- **正文已有计划仍要走一遍人工审批**。`has_detailed_plan()`（`orchestrator.py:137`）已经能识别正文里的
  实施计划并跳过 planner，但 plan-only 路径仍然一律发"等待人工审批"评论并加 `agent-planned`。当人已经
  把计划写清楚时，这一轮往返没有信息增益。
- **plan 不出来时没有回退通道**。planner 遇到信息不足的 Issue 只能硬凑一个计划，或者输出解析失败被记为
  失败。前者产出与真实意图不符的计划，后者浪费失败预算。
- **过大的 Issue 没有出口**。`runtime.max_tasks` 只限制单个 plan 的任务数，超限即失败。一个理应拆成多个
  Issue 的大需求会反复失败，直到人工发现。

本次改动为编排器补上这四件事。

## 范围

**做**：

1. **依赖门禁** —— 原生 `blockedBy` 有未完成 blocker 时，plan 和 coding 都不领取。
2. **auto-ready** —— 正文已含具体实施计划时自动加 `agent-ready`，直接进入执行，避免信息不完整的二次规划
   造成结果漂移。
3. **澄清回环** —— planner 明确表示信息不足时，把问题发回 Issue 并加标签，人工回复后自动重新规划。
4. **拆分** —— planner 判定任务过大时拆成多个独立子 Issue，父 Issue 转入人工处理。

**非目标**：

- 不做"编排器自动关闭 Issue"。父 Issue 关闭由人工决定。
- 不做自动合并、自动部署；push/PR 的边界不变。
- 不做跨 Issue 的循环依赖检测（见「已知边界」）。
- 不改变 `has_detailed_plan()` 的判定规则本身。
- 不引入 GitHub App / 分布式锁；仍是单机 SQLite + `gh` CLI。

## 术语

| 词 | 含义 |
|---|---|
| blocker | 通过 GitHub 原生 `blockedBy` 关系指向本 Issue 的前置 Issue |
| 依赖门禁 | 任一 blocker 未关闭时不领取本 Issue 的准入检查 |
| auto-ready | 正文已含具体计划时，编排器代替人工添加 `agent-ready` |
| 澄清回环 | planner 提问 → 人工回复 → 自动重新规划 的循环 |
| split child | planner 拆分产生、由编排器创建的独立子 Issue |

## 1. 标签与配置

### 1.1 新增标签

只新增一个，由编排器维护：

```python
# github.py ORCHESTRATOR_LABELS
"agent-needs-info": ("d4c5f9", "Planner needs more information"),
```

需要同步的位置：

- `src/issue_agent/github.py` 的 `ORCHESTRATOR_LABELS`（`required_label_specs` 会自动带上，preflight 无需
  单独改）
- README「初始化 GitHub Labels」的脚本
- `docs/issue-template.md` 的「发布前检查」与标签说明
- README「标签规则」一节

依赖阻塞**不新增标签**，理由见 §11 D1。

### 1.2 新增配置

```toml
[runtime]
auto_ready_with_plan = false     # 正文已含具体计划时自动加 agent-ready
allow_split = false              # 允许 planner 拆分为多个子 Issue
max_split_children = 5           # 单次拆分的子 Issue 数上限
max_clarify_rounds = 2           # 同一 Issue 最多追问轮数
clarify_ignore_authors = []      # 除本机登录外，不视为"人工回复"的评论作者
```

`config.py`：`Config` 增加同名字段，`load_config` 读取，`validate_config` 对三个正整数做与
`max_tasks` 同样的 `> 0` 校验。`clarify_ignore_authors` 做小写归一化，便于与 GitHub 登录名比较。

`allow_split` 与 `auto_ready_with_plan` 默认 `false`，保持现有行为与现有测试不变；目标仓库按需开启。

## 2. 依赖门禁

### 2.1 数据来源

`GitHub.open_issues` 的 `--json` 字段增加 `blockedBy` 与 `parent`：

```
number,title,body,labels,url,blockedBy,parent
```

`Issue` 模型（`models.py:22`）增加两个字段，保持 `frozen`：

```python
blocked_by: tuple[int, ...] = ()
parent: int | None = None
```

`blockedBy` 在 `gh issue list --json` 里返回 `{"nodes": [...], "totalCount": n}` 形态（已实测）。
`totalCount == 0` 是零成本的快路径 —— 绝大多数 Issue 在这里就判定为无依赖。

### 2.2 判定 blocker 是否完成

`totalCount > 0` 的候选才需要确认真实状态。实现时先确认 `gh issue list --json blockedBy` 的 node 是否
携带 `state`：

- 携带：直接从列表结果判定，零额外调用。
- 不携带：调用新增的 `GitHub.blocker_states(numbers) -> dict[int, bool]`，用
  `gh api graphql` 批量取 `blockedBy(first:N){nodes{number state}}`。该 GraphQL 查询已在 GitHub 上实测
  可用（返回结构与字段名均已验证），只是列表接口的 node 字段集还需在实现时确认。

同一轮轮询内按 Issue 编号缓存结果，避免重复请求。

**完成判定**：blocker 的 `state == "CLOSED"` 即为完成。编排器创建的 PR body 带 `Closes #N`，merge 即关闭
对应 Issue，所以这个信号与现有流程自洽。

### 2.3 门禁位置

在 `Orchestrator.run_once` 的两个候选循环内、`_track` 之前（`orchestrator.py:338` 的 runnable 循环与
`:362` 的 planning 循环）：

```python
if self._dependency_gate(issue):
    continue
```

**plan 与 coding 都拦**，符合"依赖完成后才进行 plan、coding 等后续步骤"。

### 2.4 豁免

已有 `agent-running` 标签，或 SQLite 行状态属于 `RUNNING_STATUSES` 的 Issue **永不被门禁拦截**。中断
的任务必须能恢复，即使依赖在其运行期间被重新打开。

这条豁免是硬性的：门禁只管"第一次准入"，不管"已在进行中的工作"。

### 2.5 告知

被拦时发**一次性**评论，列出未完成的 blocker 编号与标题。去重状态写入 SQLite，不每轮刷屏。

新增列：

```sql
blockers_notified TEXT   -- JSON 数组，已就"哪些 blocker"发过提示
```

blocker 集合发生变化时（新增或全部解除）重新提示一次：

- 全部解除 → 发一条"依赖已完成，将在下一轮继续处理"。
- 新增 blocker → 重新发一次未完成列表。

### 2.6 正文声明的一致性

正文中的 `## 依赖与风险` 一节可能写着 `Blocked by: #12`、`Depends on: #12`、`前置 Issue：#12`。

**原生 `blockedBy` 是唯一权威**。正文那行只作人类可读说明，编排器交叉核对，**不一致就发评论提示**，
不自动补写原生关系 —— 用正文反写关系是第二层自动化，出意外的成本高于收益。

匹配用一个宽松正则覆盖 `blocked by` / `depends on` / `前置 issue` / `依赖`，并容忍中英文冒号和
`#` 前缀。

注意这里与 §2.1 的快路径**不冲突但相互独立**：`totalCount == 0` 省掉的是 blocker **状态解析**（可能
需要网络调用），正文比对是纯本地字符串匹配、零 API 成本，因此**每个候选都要做** —— 正是要发现"正文写了
但原生没建"这种情形。

### 2.7 边界

- 自依赖 `#N blocked by #N`：单独识别，发评论告警，并按阻塞处理（不自旋）。
- 跨 Issue 循环依赖：不检测。相关 Issue 会一直阻塞，但 §2.5 的评论会说明原因，人工可见可处理。

## 3. planner 输出契约

`parse_plan`（`orchestrator.py:204`）目前只接受 fenced JSON 数组。扩展为三种形态：

```jsonc
// 形态一：任务列表（现有，保持不变）
[{"title": "...", "description": "..."}]

// 形态二：信息不足，提问
{"questions": ["...", "..."]}

// 形态三：任务过大，拆分
{"split": [{"title": "...", "body": "...", "depends_on": [0]}]}
```

设计要点：

- **裸数组仍是任务列表**，向后兼容，现有测试与现有 planner 输出不受影响。
- 复用现有的 `_repair_json` 与"fenced ```json 块"提取逻辑。
- 新增 `SplitChild(title, body, depends_on)` 与 `PlanOutcome(tasks, questions, split)` 数据类
  （放 `models.py`）。
- 新增 `parse_plan_output(stdout, max_tasks, max_children) -> PlanOutcome`。
  **保留现有 `parse_plan` 函数与签名不变** —— `tests/test_orchestrator.py` 直接 import 它。
- `Orchestrator._plan` 返回值由 `list[PlanTask]` 改为 `PlanOutcome`。两个调用点（`plan_only`、
  `process`）分别处理三种形态。
- `depends_on` 用**下标**表达兄弟间的先后（`[0]` 表示依赖同批第 1 个），避免解析散文里的依赖描述。
  下标越界、指向自身、或构成环 → `CommandError`，走失败路径。

## 4. auto-ready

### 4.1 触发条件

四个条件**同时**满足才触发：

1. `config.auto_ready_with_plan` 为真
2. `config.planner_agent` 已配置
3. `has_detailed_plan(issue.body)` 为真
4. `issue.parent is None`

条件 2 是**硬护栏**。`_plan` 当前逻辑是：

```python
existing_plan = bool(self.config.planner_agent) and has_detailed_plan(issue.body)
if not self.config.planner_agent or existing_plan:
    plan = [PlanTask(title=issue.title, description=issue.body)]
```

`planner_agent` 为空时，**所有** Issue 都走"正文即计划"分支（`orchestrator.py:662`）。若不加条件 2，
开启 auto-ready 会把每一个 Issue 都自动放行，等于取消整个审批环节。

条件 3+2 合并起来恰好等价于"planner-skip 分支确实被走到" —— 也就是说，**只有当计划真的逐字来自人工
撰写的正文时**才自动放行；planner LLM 生成的计划仍然需要人工审批。这正是"避免信息不完整的二次 plan
导致结果漂移"所要求的。

条件 4 排除编排器创建的子 Issue，理由见 §11 D4。

### 4.2 动作

在 `plan_only` 中 `_plan` 之后分支（`orchestrator.py:452` 附近）：

- 持久化 plan（沿用现有 `save_plan`）、写 `.agent/plan.md`
- 状态置 `PLANNED`、`current_seq=0`
- 加 `agent-ready`
- 发一条审计评论，说明"正文已含完整实施计划，按 `auto_ready_with_plan` 配置自动放行，计划即 Issue 正文"
- **不加 `agent-planned`** —— 该标签语义是"已发布计划、等待人工批准"，此路径没有等待，加了会误导
- 记 `auto_ready_applied` 事件

### 4.3 后续路径

完全复用现有流程，没有新分支：下一轮轮询 `runnable_issues` 领到 → `process` → `state.load_plan` 命中
已持久化的 plan → 复用，**不重新调用 planner**。

加上 `agent-ready` 后，`unassigned_issues`（`github.py:89`）会立刻把它过滤出 plan 池，不会重复领取。

### 4.4 测试影响

`test_plan_only_reuses_detailed_issue_and_waits_for_ready`（`tests/test_orchestrator.py:389`）在默认
`auto_ready_with_plan = false` 下行为不变、继续通过。新增一条开启后的对照测试。

## 5. 澄清回环

### 5.1 流程

1. plan-only 领到 Issue → planner 返回 `{"questions": [...]}`。
2. 编排器格式化后发评论、加 `agent-needs-info`、状态写回 `PENDING`、`clarify_rounds += 1`、
   记下提问评论的时间戳 marker。**不写 plan**，所以 `claim_for_planning`（`state.py:195`）下次仍能领。
3. 因为 `agent-needs-info` 是 `agent-` 前缀，`unassigned_issues` 自动把它挡在 plan 池外 ——
   "别自己刷自己"不需要额外加锁。
4. 轮询时对**等待中的** Issue 拉评论，发现**非本机登录**、时间戳晚于 marker 的评论 → 移除
   `agent-needs-info`，下一轮自动重新入队 plan。
5. 重新规划时 `make_plan_prompt` 注入 `## Clarification so far` 段（提问 + 人工回复原文），
   并在 prompt 中说明"信息已补充，尽可能给出计划；仍不足才继续提问"。
6. `clarify_rounds` 达到 `max_clarify_rounds` 就收手：保留 `agent-needs-info`，发评论说明需要人工补充后
   执行 `issue-agent reset`，不再自动重新规划。

### 5.1.1 "等待中的 Issue" 怎么找

`run_once` 新增一个检测循环，**数据源是 SQLite 而不是 GitHub**：扫描 `state.rows()` 中
`clarify_marker` 非空、且 `clarify_rounds < max_clarify_rounds` 的行，对这些 Issue 编号逐个拉评论。

这样做有三个好处：不需要为"哪些 Issue 在等回复"新增 GitHub 查询；轮数用尽的行天然被排除在循环外
（**检测器直接跳过，不会再因为人工回复而移除标签**），这正是第 6 步要的语义；`reset()` 清空这两列后
该 Issue 自动重新纳入检测。

该循环与既有的 runnable / planning 循环并列，同样在 `self.running` 里去重，避免同一 Issue 并发处理。

### 5.2 prompt 改动

`agents.py:155` 的 `make_plan_prompt` 增加：

- 新的输出说明：信息不足以规划出具体任务时，返回 `{"questions": ["..."]}` 而不是猜测；问题应当具体、
  可回答，1–3 个。
- 可选参数 `clarification: str = ""`，非空时追加 `## Clarification so far` 段。

`_plan` 调用时按需传入（从评论里取到的 Q&A 转录）。

### 5.3 识别"人工回复"

新增：

```python
async def GitHub.viewer_login() -> str          # gh api user --jq .login，启动时取一次
async def GitHub.comments(number) -> list[Comment]   # gh issue view N --json comments
```

`Comment(author, created_at, body)`。

人工回复的判定：`created_at` 晚于 marker **且** `author` 不等于本机登录名 **且** 不在
`clarify_ignore_authors` 里。

本机登录名在 `Orchestrator.__init__` 中惰性获取一次并缓存；获取失败（无凭据等）时记警告，此时**不**
自动检测回复 —— 宁可让人工手动移除标签，也不能把编排器自己的问题评论当成人的回答而陷入自问自答。

`viewer_login()` 在 `dry_run` 下不得发起请求，与 `labels()` / `create_pr()` 的既有模式一致 —— 代价是
`dry_run` 下拿不到本机登录名，此时**关闭回复检测**（`dry_run` 本就不落任何状态，不影响正常路径）。

### 5.4 持久化

新增列（沿用 `state.py:52` 起的 `ALTER TABLE ADD COLUMN` 迁移模式）：

```sql
clarify_rounds INTEGER NOT NULL DEFAULT 0
clarify_marker TEXT
```

`StateStore.update()` 的 `allowed` 白名单**不收**这两个字段（该白名单是通用写入入口，收进去容易被顺手
写坏状态），改用专用方法：`record_clarify_round(issue_number, marker)`、`clear_clarify(issue_number)`。

`reset()` 需要一并清空这两个字段，让被搁置的 Issue 重新获得完整的追问预算。

### 5.5 状态与恢复

提问后状态是 `PENDING`，所以进程重启时 `recover_interrupted` 不会把它计为中断失败（它只处理
`PLANNING` 与 `RUNNING_STATUSES`）。这是刻意的：等待人工回复不是失败。

## 6. 拆分

### 6.1 流程

1. planner 返回 `{"split": [...]}`，子 Issue 的 `body` 按 `docs/issue-template.md` 写全。
2. `GitHub.create_issue(title, body, labels) -> (number, url)`，`dry_run` 下返回假编号且不发请求
   （`dry_run` 本就不写 SQLite，假编号不会被持久化）。
3. 每个子 Issue 创建后：
   - 原生挂到父 Issue：`gh issue edit <child> --parent <parent>`，GitHub UI 会显示子任务进度
   - 复制父 Issue 的**非 `agent-*`** 标签（如 `enhancement`）
   - **绝不加 `agent-ready`**
   - 按 `depends_on` 建立兄弟间关系：`gh issue edit <child> --add-blocked-by <other>`
4. 父 Issue：状态置为新枚举 `TaskStatus.SPLIT`，加 `human-review`，发评论列出所有子 Issue 编号与链接，
   移除 `agent-running` / `agent-planned`。
5. `TaskStatus.SPLIT` 不在 `claim`（`state.py:180-186`）的允许集合里，`_eligible` 对它也返回 False，
   所以父 Issue 自然搁置；人工 `reset` 后可重新规划 —— 这是"不满意拆分结果"的退路。

### 6.2 幂等

这是拆分最大的风险点：**LLM 重跑会产出不同的标题**，若按标题判断"是否已创建"就会重复创建。

做法：

1. 决策先落盘 —— 先把完整 split 载荷写入 SQLite 新列 `split`（JSON），再开始创建。
2. **每成功创建一个子 Issue 就立刻把编号写回该记录**。
3. 崩溃或 `gh issue create` 失败后重试时，只补没建成的那些（记录里 `number` 为空或 0 的项），
   已建成的直接跳过。
4. 已有 split 记录时 `_plan` **不再调用 planner**，直接进入"补建 + 收尾"。

新增列：

```sql
split TEXT   -- JSON: {"children": [{"index":0,"title":...,"body":...,"depends_on":[],"number":0,"url":""}]}
```

### 6.3 失败处理

单个 `gh issue create` 失败按普通 `CommandError` 处理：记失败、走 `_park_or_requeue`。因为 §6.2 已保证
幂等，重试不会重复创建。

部分成功（如建了 2 个、第 3 个失败）时，父 Issue **不**转入 `human-review`，保持失败重试语义；评论里列出
已创建的编号，让人工能看到当前状态。

### 6.4 边界

- `max_split_children` 校验在 `parse_plan_output` 内完成，超限即 `CommandError`（与 `max_tasks` 一致的
  处理方式）。
- `allow_split = false` 时，收到 `{"split": ...}` 视为解析失败，错误信息里说明"拆分未启用"。
- 子 Issue 的 `labels` 只复制非 `agent-*` 标签；父 Issue 的 `agent:<name>` 路由标签**不复制**，让子
  Issue 走默认 Agent（人可以在放行时显式指定）。

## 7. 状态机与持久化汇总

```text
新增 TaskStatus.SPLIT          -- 父 Issue 已拆分为子 Issue，等人工处理

tasks 新增列：
  blockers_notified TEXT       -- 已提示过的 blocker 集合
  clarify_rounds      INTEGER NOT NULL DEFAULT 0
  clarify_marker      TEXT
  split               TEXT     -- 拆分决策 + 已创建编号
```

迁移沿用 `state.py` 既有的 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 模式，幂等、可在旧库上直接跑。

**持久化顺序**保持仓库既有原则：先写 SQLite，再做 GitHub Label 变化。拆分是唯一例外 —— 子 Issue 编号
在创建**之后**才知道，所以先落盘决策、创建后立刻回填编号。

## 8. 审计与日志

新增 `issue_log` 事件（`IssueLog.event`）：

| 事件 | 时机 |
|---|---|
| `dependency_blocked` | 因未完成的 blocker 跳过领取 |
| `dependency_cleared` | blocker 全部解除 |
| `dependency_body_mismatch` | 正文声明的 blocker 与原生关系不一致 |
| `auto_ready_applied` | auto-ready 放行 |
| `clarify_requested` | planner 提问，已发评论并加标签 |
| `clarify_answered` | 检测到人工回复，准备重新规划 |
| `clarify_exhausted` | 追问轮数用尽，搁置 |
| `split_created` | 拆分完成，含子 Issue 编号 |
| `split_partial` | 部分子 Issue 创建失败 |
| `split_reused` | 复用已落盘的拆分决策 |

README 的「Issue 执行与 Review 日志」一节同步补充这些事件。

## 9. 文档更新

- `README.md`：标签规则、发布流程、配置项、日志事件、当前边界。
- `docs/development-flow.md`：mermaid 流程图增加依赖门禁、auto-ready、澄清回环、拆分四条支路。
- `docs/issue-template.md`：`agent-needs-info` 说明；`## 依赖与风险` 一节改为引导使用 GitHub 原生
  `blockedBy`（网页 Issue 侧栏的 "Blocked by" 关系），并说明正文那行只作说明、不一致时会收到提示。

## 10. 测试策略

沿用现有模式：`tests/test_orchestrator.py` 已有假 `gh` 调用的测试脚手架，GitHub 层全部走它，不打真实 API。

**依赖门禁**
- blocker 未关闭 → 不领取、发一次性评论
- blocker 全部关闭 → 领取
- `agent-running` 的 Issue 不被门禁拦截
- 去重：同一 blocker 集合只提示一次；blocker 集合变化后重新提示
- 正文声明与原生关系不一致 → 发提示评论
- 自依赖 → 告警且不自旋

**契约解析**
- 三种 JSON 形态各自解析正确
- 畸形输入（缺字段、`depends_on` 越界/自指/成环、超过 `max_split_children`）→ `CommandError`
- `allow_split = false` 时 split 输出被拒

**auto-ready**
- 四个触发条件各自的否分支都不放行（配置关、`planner_agent` 空、正文无计划、有 parent）
- 触发时加 `agent-ready` 且**不加** `agent-planned`
- 默认 `false` 时现有行为完全不变（`test_plan_only_reuses_detailed_issue_and_waits_for_ready` 继续通过）
- 放行后 `process` 复用已持久化的 plan，不重新调用 planner

**澄清回环**
- 提问 → 加 `agent-needs-info`、状态 `PENDING`、未写 plan
- `agent-needs-info` 使 Issue 退出 plan 池
- 人工回复 → 移除标签、重新入队、prompt 注入 Q&A
- 本机登录名与 `clarify_ignore_authors` 的评论不触发
- 达到 `max_clarify_rounds` → 保留标签、发搁置评论，且**检测器此后不再响应人工回复**（§5.1.1）
- `reset()` 清空追问计数后，该 Issue 重新纳入检测
- `viewer_login()` 失败时不自动检测回复
- `dry_run` 下不发 `gh api user` 请求，且关闭回复检测

**拆分**
- 创建顺序、父子关系、兄弟 `blockedBy`、标签复制（非 `agent-*`）
- **中途崩溃后重试不重复创建**
- `dry_run` 下不发任何请求、返回假编号
- 父 Issue 置 `SPLIT` 后不可领取，`reset` 后可重新规划
- 部分失败时父 Issue 保持可重试

## 11. 已确认的设计决策

以下四项在 brainstorming 中逐条确认过，记录结论与理由。

**D1｜依赖阻塞不新增标签，只发一次性评论。**
理由：原生 `blockedBy` 已是唯一真相源，再镜像一个 `agent-*` 标签会漂移；而且 `agent-` 前缀会把 Issue
踢出 plan 池（`unassigned_issues` 的过滤规则），一旦忘了在解除时移除就是静默死锁。
代价：Issue 列表里不能一眼看出"在等依赖"，需要点进 Issue 看评论。

**D2｜父 Issue 不自动关闭，只进 `human-review`。**
理由：关闭不可逆；且 GitHub 对"有未完成 blocker 的 Issue 能否关闭"的行为未经验证，不适合作为自动化的
前置假设。人工看完子 Issue 后自行关闭。

**D3｜澄清与拆分只在 plan-only 路径生效。**
若人先加了 `agent-ready`（Issue 从未进入 plan-only），coding 路径的 `_plan` 也可能拿到
`questions`/`split`。此时：移除 `agent-ready`、加 `agent-needs-info`、发评论说明应改走 plan-only 流程。
改人类标签有一点越界，但仓库既有 `_park_after_review_failure`（`orchestrator.py:645`）已移除
`ready_label`，有先例。

**D4｜拆分产生的子 Issue 明确排除出 auto-ready。**
理由：拆分是结构性决策，一次会开出多个任务，保留一个人工合闸点更稳。识别方式是原生 `parent` 关系
（`issue.parent is not None`）。
附带效果：人工手工创建的子 Issue 同样被排除 —— 这符合"父任务下的人工确认"直觉。
代价：每个子 Issue 多一次人工加 `agent-ready` 的操作。

**已否决**：不做"编排器自动关闭父 Issue"；不做基于正文的依赖自动补写。

## 12. 建议的实现顺序

每一步都可独立提交、独立测试，且不破坏现有行为（新增能力默认关闭）：

1. **配置与模型** —— 新配置项、`Issue.blocked_by/parent`、`SplitChild`/`PlanOutcome`、`TaskStatus.SPLIT`。
2. **契约解析** —— `parse_plan_output` 三形态 + 校验，配套单元测试。`_plan` 改返回 `PlanOutcome`，
   两个调用点先只处理 `tasks` 形态，`questions`/`split` 暂时按解析失败处理。此时行为与现状等价。
3. **依赖门禁** —— 只读部分（`blockedBy`/`parent` 拉取、`blocker_states`、门禁与豁免），先不做通知。
4. **依赖门禁通知** —— `blockers_notified` 列、一次性评论、正文一致性提示。
5. **auto-ready** —— `plan_only` 分支 + 测试。
6. **澄清回环** —— prompt 改动、`comments`/`viewer_login`、状态列、轮询检测、轮数上限。
7. **拆分** —— `create_issue`、幂等落盘、父子与兄弟关系、父 Issue 收尾。
8. **文档与流程图** —— README、development-flow、issue-template。

3–5 之间无依赖，可按需调整顺序。6 依赖 2，7 依赖 2 与 6 的 `comments`/`viewer_login` 基础设施。

## 13. 已知边界

- **跨 Issue 循环依赖不检测**：相关 Issue 会一直阻塞，靠 §2.5 的评论让人工发现。
- **"人工回复"的判定是启发式的**：以"非本机登录名的评论"为准。若仓库里有其他自动化 bot 评论，需要
  手工加进 `clarify_ignore_authors` 或关闭该功能。
- **单个 Issue 对应一个分支/PR 的模型不变**：拆分产生的是多个独立 Issue，各自走完整流水线。
- **父 Issue 的 `human-review` 与 PR 完成后的 `human-review` 语义重叠**：两者都表示"等人工看"，靠评论
  内容区分。这是复用既有标签的代价，换来的是不新增标签。
- **`blockedBy` 的 node 是否携带 `state` 需要在实现时确认**（见 §2.2），不携带则走 GraphQL 回退路径；
  两条路径的行为已被设计为对调用方透明。
