# Coding Agent Orchestrator

一个本地常驻、GitHub Issue 驱动的通用 Coding Agent 调度器。支持 Codex、Claude Code、OpenCode，以及任意可从 CLI 调用的 Agent。

## 工作流

1. 轮询带 `agent-ready` 标签的 GitHub Issues。
2. 根据 `agent:<name>` 标签选择 Agent；未指定则使用默认 Agent。
3. 从 `origin/main` 创建独立分支和 Git worktree。
4. 把 Issue 保存为 `.agent/task.md`，Agent 只负责修改代码。
5. orchestrator 独立执行检查；失败时把错误反馈给同一 Agent，最多重试三次。
6. 可选用另一个 Agent 做只读交叉 Review。
7. orchestrator 统一 commit、push、创建 PR，并停在 `human-review`。

它不会自动 merge、部署生产、运行生产迁移或修改 secrets。

## 快速开始

要求：Python 3.11+、Git、GitHub CLI，以及至少一个 Agent CLI。

```bash
git clone https://github.com/chengcz/coding-agent-orchestrator.git
cd coding-agent-orchestrator
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
gh auth login
cp orchestrator.example.toml orchestrator.toml
```

编辑 `orchestrator.toml`：

- `runtime.repo`：目标项目的主 checkout，必须已有 `origin`。
- `runtime.worktrees`：每个 Issue 的隔离工作区根目录。
- `github.repo`：当前默认是 `chengcz/coding-agent-orchestrator`。
- `checks.commands`：目标项目真实的验收命令。
- 启用已经安装且完成认证的 Agent。

先运行一次：

```bash
cao --config orchestrator.toml once
cao --config orchestrator.toml status
```

确认无误后常驻：

```bash
cao --config orchestrator.toml serve
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

## 目标项目约定

目标项目应包含 `AGENTS.md`，Claude 项目可增加一个很短的 `CLAUDE.md`，只引用 `AGENTS.md`，避免规则分叉。建议把目标项目的检查统一为 `scripts/check.sh`，然后配置：

```toml
[checks]
commands = ["./scripts/check.sh"]
```

`.agent/task.md` 由 orchestrator 创建，通常应加入目标项目的 `.gitignore`。如果希望 PR 保留任务快照，则不要忽略。

## 持续运行

Linux 使用 [deploy/coding-agent-orchestrator.service](deploy/coding-agent-orchestrator.service)，修改用户和路径后：

```bash
sudo cp deploy/coding-agent-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coding-agent-orchestrator
journalctl -u coding-agent-orchestrator -f
```

macOS 使用 [deploy/com.cao.orchestrator.plist](deploy/com.cao.orchestrator.plist)，修改路径后放入 `~/Library/LaunchAgents/` 并用 `launchctl bootstrap` 加载。

### Docker Compose

容器方式适合把 orchestrator、目标仓库和 Agent CLI 全部放在同一持久环境中。先复制配置并把容器内路径设为：

```toml
[runtime]
repo = "/workspace"
worktrees = "/data/worktrees"
state_db = "/data/state/orchestrator.sqlite3"
```

然后创建 `.env`（不要提交）：

```dotenv
GH_TOKEN=github_pat_xxx
TARGET_REPO=/absolute/path/to/target/repository
```

基础镜像只安装 Python、Git 和 GitHub CLI。请在 `Dockerfile` 中安装并认证实际使用的 Agent CLI，再执行：

```bash
docker compose build
docker compose up -d
docker compose logs -f orchestrator
```

目标仓库与 worktree 必须使用容器内稳定路径；迁移宿主机目录后应重新创建 worktree。生产常驻更推荐 systemd：它能直接复用宿主机已有的 Agent CLI 登录态。

### 运维检查

- `cao --config /etc/cao/orchestrator.toml status` 查看持久状态。
- 服务重启会把中断的内部状态标记为可重试，并重新领取仍带 `agent-running` 的 Issue。
- SQLite、worktrees 和目标仓库必须位于持久磁盘，并定期备份 SQLite。
- 只运行一个 orchestrator 实例；当前版本的资源锁是进程内锁，不支持多机竞争。

## 安全边界

- 为 GitHub token 只授予目标仓库的 Issues/Contents/Pull requests 所需权限。
- Agent 在 worktree 中运行；Codex 示例使用 `workspace-write` sandbox。
- Agent 不掌握 GitHub 操作流程；push/PR 由 orchestrator 控制。
- 不自动 merge main，不自动部署，不接触生产数据库和 secrets。
- 推荐专用 OS 用户运行；不要把生产凭据传给 worker。
- SQLite 保存任务状态，常驻进程重启后不会重复领取已经处于运行或人审状态的任务。

## 当前边界与下一阶段

MVP 使用 `gh` CLI 和 SQLite，适合单机 1–5 个并发 worker。多机部署时再替换为 GitHub App + PostgreSQL/Redis 分布式锁，并增加心跳、取消、PR review 循环和指标监控。不要在单机版上直接启动多个 orchestrator 实例。

Codex 的非交互自动化入口是 `codex exec`；官方也建议非交互运行使用 workspace-write sandbox。GitHub Actions 中可另行使用官方 Codex Action，但它不是本地常驻调度器的必需依赖。
