# Coding Agent Orchestrator

一个本地常驻、GitHub Issue 驱动的通用 Coding Agent 调度器。支持 Codex、Claude Code、OpenCode，以及任意可从 CLI 调用的 Agent。

## 平台定位

- **Linux**：7×24 生产常驻的首选平台，使用 systemd 托管。
- **macOS**：7×24 常驻的支持平台，使用 launchd 托管。
- **Windows**：仅作为本项目的开发、测试环境，不作为不间断任务的部署目标。

调度器保留 Windows 命令执行兼容性，以便当前开发验证；生产运维、进程托管和故障恢复以 Linux/macOS 为准。

## 工作流

1. 轮询带 `agent-ready` 标签的 GitHub Issues。
2. 根据 `agent:<name>` 标签选择 Agent；未指定则使用默认 Agent。
3. 从 `origin/main` 创建独立分支和 Git worktree。
4. 把 Issue 保存为 `.agent/task.md`，Agent 只负责修改代码。
5. orchestrator 独立执行检查；失败时把错误反馈给同一 Agent。
6. 可选用另一个 Agent 做只读交叉 Review；Review 要求修改时也反馈给实现 Agent。编码、检查和
   Review 共用 `max_attempts` 尝试预算。
7. orchestrator 统一 commit、push、创建 PR，并停在 `human-review`。

它不会自动 merge、部署生产、运行生产迁移或修改 secrets。

## 快速开始

### 系统依赖

生产运行支持 Linux 和 macOS，需要：

- Python 3.11 或更高版本，包含 `venv` 和 `pip`。
- Git 2.30 或更高版本，并支持 `git worktree`。
- GitHub CLI（`gh`），已对目标仓库完成认证。
- 至少一个可非交互运行的 Coding Agent CLI，例如 Codex、Claude Code 或 OpenCode。
- 目标仓库检查命令所需的构建工具，例如 Node.js、Go、Rust、Java 或数据库客户端。
- Linux 使用 systemd；macOS 使用 launchd。
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
git clone https://github.com/chengcz/coding-agent-orchestrator.git
cd coding-agent-orchestrator
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
gh auth login
cp orchestrator.example.toml autocode.toml
```

编辑 `autocode.toml`：

- `runtime.repo`：目标项目的主 checkout，必须已有 `origin`。
- `runtime.worktrees`：每个 Issue 的隔离工作区根目录。
- `github.repo`：当前默认是 `chengcz/coding-agent-orchestrator`。
- `checks.commands`：目标项目真实的验收命令。
- 启用已经安装且完成认证的 Agent。

先运行一次：

```bash
autocode --config autocode.toml once
autocode --config autocode.toml status
```

确认无误后常驻：

```bash
autocode --config autocode.toml serve
```

## Agent 配置

内置的是“配置式适配器”，因此不需要为每个厂商维护一套调度逻辑：

```toml
[agents.codex]
command = "codex exec --sandbox workspace-write -"
max_workers = 2

[agents.claude]
command = "claude -p"
max_workers = 1

[agents.custom]
command = "your-agent --prompt {prompt}"
```

命令不含 `{prompt}` 时，prompt 通过 stdin 发送，避免 Issue 太长导致 shell 参数限制；包含占位符时会作为一个参数直接传入，不经过 shell 展开。

Issue 标签示例：

- `agent-ready`：允许调度。
- `agent:codex` / `agent:claude`：选择实现 Agent。
- `resource:database-schema`：全局串行，避免 Alembic 多头迁移。
- `agent-running`、`agent-failed`、`human-review`：由调度器维护。

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

## 目标项目约定

目标项目应包含 `AGENTS.md`，Claude 项目可增加一个很短的 `CLAUDE.md`，只引用 `AGENTS.md`，避免规则分叉。建议把目标项目的检查统一为 `scripts/check.sh`，然后配置：

```toml
[checks]
commands = ["./scripts/check.sh"]
```

`.agent/task.md` 由 orchestrator 创建，通常应加入目标项目的 `.gitignore`。如果希望 PR 保留任务快照，则不要忽略。

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
   .venv/bin/autocode --config bioagent.toml --verbose serve
   ```

6. Agent 完成实现和检查后，编排器会提交分支、推送并创建 PR，同时把 Issue 标记为
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
- `agent-running`、`agent-failed`、`human-review`：由编排器维护，不要手工用于发布任务。
- `bug`、`enhancement`、`documentation`：描述任务类型，可与 Agent 标签组合。

不要同时添加多个 `agent:<name>` 标签。一个 Issue 应对应一个可独立审查的 PR；大型需求应拆成有依赖关系的多个 Issue。

## Agent 与 Review

默认实现 Agent 是 Codex。Issue 使用 `agent:claude` 时由 Claude Code 实现。当前配置使用 Codex
做只读 Review；因此 Claude 实现的任务会得到跨 Agent 复审。Review 未明确给出批准结论时，任务会被标记为失败并等待人工处理。

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

填写并检查完上述内容后，在 GitHub Issue 网页右侧的 **Labels** 区域发布任务：

1. 打开 `chengcz/bioagent` 仓库的 **Issues** 页面，点击 **New issue**。
2. 填写标题和本模板中的所有必填章节，然后点击 **Create** 或 **Submit new issue**。
3. 在 Issue 右侧找到 **Labels**，点击齿轮图标或标签区域。
4. 选择且只选择一个实现 Agent：
   - `agent:codex`：由 Codex 实现；未选择 Agent 标签时也默认使用 Codex。
   - `agent:claude`：由 Claude Code 实现。
5. 根据任务性质选择 `bug`、`enhancement` 或 `documentation` 等普通标签。
6. 确认任务范围、依赖和验收标准完整后，最后添加 `agent-ready`。
7. 关闭标签选择框。无需额外按钮；标签保存后，前台编排器会在下一次轮询时领取任务。

如果列表中没有 `agent-ready`：

1. 打开仓库的 **Issues** 页面。
2. 点击页面上方的 **Labels**。
3. 点击 **New label**。
4. Name 填写 `agent-ready`，Description 可填写 `Ready for coding-agent implementation`，颜色可使用 `0e8a16`。
5. 点击 **Create label**，返回 Issue 后按上述步骤添加该标签。

发布前检查：

- [ ] Issue 可以由一个独立 PR 完成。
- [ ] 已写明可执行的验收标准和测试要求。
- [ ] 前置 Issue 已完成；否则暂时不要添加 `agent-ready`。
- [ ] 没有同时添加 `agent:codex` 和 `agent:claude`。
- [ ] 没有手工添加 `agent-running`、`agent-failed` 或 `human-review`；这些标签由编排器维护。

需要暂停尚未领取的任务时，从 Issue 右侧 **Labels** 中移除 `agent-ready`。任务已出现
`agent-running` 后不要靠修改标签强行停止，应先安全停止前台编排器并检查任务状态。

## 部署与运维指南

本项目只把 Linux 和 macOS 作为 7×24 部署目标。Windows 用于开发和测试，不承载常驻 Worker。当前版本面向单机运行：一个仓库对应一个 orchestrator 实例；一台机器可以运行多个相互隔离的实例。

### 1. 部署前规划

每个仓库必须拥有独立的主 checkout、worktree 根目录、SQLite 文件和配置文件：

```text
/opt/coding-agent-orchestrator/       程序与虚拟环境
/etc/autocode/                             配置和非敏感环境文件
/srv/autocode/repo-a/repo/                 repo-a 主 checkout
/srv/autocode/repo-a/worktrees/            repo-a 工作区
/var/lib/autocode/repo-a/state.sqlite3     repo-a 状态
/srv/autocode/repo-b/repo/                 repo-b 主 checkout
/srv/autocode/repo-b/worktrees/            repo-b 工作区
/var/lib/autocode/repo-b/state.sqlite3     repo-b 状态
```

禁止两个实例共享同一 SQLite 文件、worktree 根目录或目标仓库。不同仓库如果会修改同一数据库 schema、共享生成文件或占用独占测试设备，需要在 orchestrator 之外提供机器级锁；当前 `resource:database-schema` 只在单个进程内生效。

### 2. 系统依赖

- Python 3.11+，包含 `venv`、`pip` 和 SQLite 支持。
- Git 2.30+，以及对目标仓库的 fetch/push 权限。
- GitHub CLI，并完成目标仓库认证。
- 至少一个支持非交互调用的 Agent CLI。
- 目标仓库自身的编译、测试和格式检查工具。
- Linux 使用 systemd；macOS 使用 launchd。

Debian/Ubuntu 的基础包示例：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git ca-certificates
```

macOS 可通过 Homebrew 安装基础工具：

```bash
brew install python git gh
```

安装 Agent CLI 和 GitHub CLI 时应遵循各自官方说明。必须使用将要运行服务的同一个 OS 用户完成认证，并验证非交互命令在该用户环境中可用。

### 3. 安装 orchestrator

Linux 推荐创建专用账户：

```bash
sudo useradd --create-home --shell /bin/bash agent
sudo install -d -o agent -g agent /opt/coding-agent-orchestrator /srv/autocode /var/lib/autocode
sudo install -d -o root -g agent -m 0750 /etc/autocode
```

以该账户克隆和安装：

```bash
sudo -u agent git clone https://github.com/chengcz/coding-agent-orchestrator.git /opt/coding-agent-orchestrator
sudo -u agent python3 -m venv /opt/coding-agent-orchestrator/.venv
sudo -u agent /opt/coding-agent-orchestrator/.venv/bin/pip install /opt/coding-agent-orchestrator
```

如果这台机器也用于维护和验证 orchestrator 本身，可改为安装 `/opt/coding-agent-orchestrator[dev]`。

目标仓库也由该账户克隆，且必须配置 `origin`：

```bash
sudo -u agent git clone git@github.com:OWNER/repo-a.git /srv/autocode/repo-a/repo
```

### 4. 每仓库配置

将示例配置复制为 `/etc/autocode/repo-a.toml`，至少修改：

```toml
[runtime]
repo = "/srv/autocode/repo-a/repo"
worktrees = "/srv/autocode/repo-a/worktrees"
state_db = "/var/lib/autocode/repo-a/state.sqlite3"
poll_seconds = 60
max_workers = 2
max_attempts = 3
default_agent = "codex"

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
sudo install -d -o agent -g agent /var/lib/autocode/repo-a /srv/autocode/repo-a/worktrees
sudo chown root:agent /etc/autocode/repo-a.toml
sudo chmod 0640 /etc/autocode/repo-a.toml
```

先以前台单次模式验收，确认 GitHub 和 Agent 登录态、仓库权限及检查命令均正常：

```bash
sudo -u agent /opt/coding-agent-orchestrator/.venv/bin/autocode --config /etc/autocode/repo-a.toml once
sudo -u agent /opt/coding-agent-orchestrator/.venv/bin/autocode --config /etc/autocode/repo-a.toml status
```

### 5. Linux：systemd

单仓库可以安装 `autocode.service`。多仓库推荐模板服务：

```bash
sudo cp deploy/autocode@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autocode@repo-a
sudo systemctl enable --now autocode@repo-b
```

服务名 `%i` 对应 `/etc/autocode/%i.toml`。常用操作：

```bash
systemctl status autocode@repo-a
journalctl -u autocode@repo-a -f
sudo systemctl restart autocode@repo-a
sudo systemctl stop autocode@repo-a
```

模板默认只允许写入 `/srv/autocode`、`/var/lib/autocode` 和 Agent 常见的用户级缓存/配置目录。首次启动前创建实际 Agent CLI 所需的目录；若仓库或 Agent 状态位于其他位置，应精确修改 `ReadWritePaths`，不要放宽为整个根目录或用户主目录。

### 6. macOS：launchd

将项目放到稳定的绝对路径，修改 `com.autocode.orchestrator.plist` 中的程序、配置、工作目录和日志路径。每个仓库复制一份 plist，并为 `Label`、配置文件和日志使用唯一名称，例如：

```text
~/Library/LaunchAgents/com.autocode.repo-a.plist
~/Library/LaunchAgents/com.autocode.repo-b.plist
```

加载并检查：

```bash
mkdir -p /opt/coding-agent-orchestrator/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.autocode.repo-a.plist
launchctl kickstart -k gui/$(id -u)/com.autocode.repo-a
launchctl print gui/$(id -u)/com.autocode.repo-a
tail -f /opt/coding-agent-orchestrator/logs/repo-a.stdout.log
```

LaunchAgent 只在用户登录会话中运行。需要开机后无人登录持续运行时，应由管理员创建系统级 LaunchDaemon，明确设置运行用户和最小文件权限，并验证 Agent CLI 的认证机制在无 GUI/钥匙串交互时仍可用。

### 7. 多 Worker 与容量

`runtime.max_workers` 限制单仓库总并发，`agents.<name>.max_workers` 限制特定 Agent 并发，实际并发取二者允许的较小值。一台机器上所有实例的 Worker 总数还应受 CPU、内存、磁盘 I/O 和 API 限额约束。

建议从每仓库 1 个 Worker 开始，观察完整编译和测试的峰值资源后再增加。一般可按每个 Worker 至少 2 个 CPU 核心、2–4 GB 内存估算；大型项目应以实测为准。确保磁盘容量可以容纳主 checkout、并行 worktrees、构建产物和依赖缓存。

### 8. GitHub 与凭据

GitHub 凭据只授予目标仓库所需的 Issues、Contents 和 Pull requests 权限。不要把生产密钥、部署凭据或数据库管理员凭据放入服务环境。建议为常驻机器使用独立机器人账户，并通过以下命令验证身份：

```bash
sudo -u agent gh auth status
sudo -u agent git -C /srv/autocode/repo-a/repo fetch origin main
```

如需通过环境变量提供凭据，将其放在 root 可读的凭据管理位置。`/etc/autocode/environment` 和 `%i.env` 只适合由权限严格控制的环境文件，不应提交到 Git。

### 9. 升级与回滚

升级前先停止实例，避免在 Worker 执行期间替换代码：

```bash
sudo systemctl stop 'autocode@*'
sudo -u agent git -C /opt/coding-agent-orchestrator pull --ff-only
sudo -u agent /opt/coding-agent-orchestrator/.venv/bin/pip install /opt/coding-agent-orchestrator
sudo systemctl start autocode@repo-a autocode@repo-b
```

安装了 `[dev]` 依赖时，上线前运行项目自身检查：

```bash
cd /opt/coding-agent-orchestrator
.venv/bin/ruff check .
.venv/bin/pytest -q
```

回滚时检出已验证的 Git 提交、重新安装包并重启服务。不要删除 SQLite 或 worktree 来代替状态恢复。

### 10. 备份与恢复

备份范围包括每个仓库的 SQLite 状态文件、配置文件和必要的 Agent CLI 认证资料。仓库代码可从 GitHub 恢复，worktree 通常无需备份。复制 SQLite 前应停止对应实例，或使用 SQLite 在线备份工具生成一致快照。

进程重启时，调度器会把中断状态标记为可重试，并重新读取仍带 `agent-running` 标签的 Issue。恢复后检查：

```bash
autocode --config /etc/autocode/repo-a.toml status
git -C /srv/autocode/repo-a/repo worktree list
gh issue list --repo OWNER/repo-a --label agent-running
```

### 11. 故障排查

- 服务反复重启：查看 journal 或 launchd stderr，检查配置路径和 Python 环境。
- 无法领取 Issue：确认标签名称、GitHub 仓库名和 `gh auth status`。
- 无法创建 worktree：检查 `origin/<base_branch>`、目录权限和残留 worktree。
- Agent 命令找不到：服务不会读取交互式 shell 配置，应在 TOML 中使用绝对路径或显式设置受控 PATH。
- push/PR 失败：验证服务账户的 SSH key 或 GitHub token，以及仓库分支策略。
- 任务长期占用：检查 Agent 超时、目标项目测试超时及机器资源；不要直接启动第二个同仓库实例绕过阻塞。

当前版本不支持多机调度、跨实例锁、自动合并或生产部署。需要高可用多机运行时，应先引入集中式状态库、租约和分布式锁设计。


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
