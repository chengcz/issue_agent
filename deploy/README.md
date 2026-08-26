# 部署与运维指南

本项目只把 Linux 和 macOS 作为 7×24 部署目标。Windows 用于开发和测试，不承载常驻 Worker。当前版本面向单机运行：一个仓库对应一个 orchestrator 实例；一台机器可以运行多个相互隔离的实例。

## 1. 部署前规划

每个仓库必须拥有独立的主 checkout、worktree 根目录、SQLite 文件和配置文件：

```text
/opt/coding-agent-orchestrator/       程序与虚拟环境
/etc/cao/                             配置和非敏感环境文件
/srv/cao/repo-a/repo/                 repo-a 主 checkout
/srv/cao/repo-a/worktrees/            repo-a 工作区
/var/lib/cao/repo-a/state.sqlite3     repo-a 状态
/srv/cao/repo-b/repo/                 repo-b 主 checkout
/srv/cao/repo-b/worktrees/            repo-b 工作区
/var/lib/cao/repo-b/state.sqlite3     repo-b 状态
```

禁止两个实例共享同一 SQLite 文件、worktree 根目录或目标仓库。不同仓库如果会修改同一数据库 schema、共享生成文件或占用独占测试设备，需要在 orchestrator 之外提供机器级锁；当前 `resource:database-schema` 只在单个进程内生效。

## 2. 系统依赖

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

## 3. 安装 orchestrator

Linux 推荐创建专用账户：

```bash
sudo useradd --create-home --shell /bin/bash agent
sudo install -d -o agent -g agent /opt/coding-agent-orchestrator /srv/cao /var/lib/cao
sudo install -d -o root -g agent -m 0750 /etc/cao
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
sudo -u agent git clone git@github.com:OWNER/repo-a.git /srv/cao/repo-a/repo
```

## 4. 每仓库配置

将示例配置复制为 `/etc/cao/repo-a.toml`，至少修改：

```toml
[runtime]
repo = "/srv/cao/repo-a/repo"
worktrees = "/srv/cao/repo-a/worktrees"
state_db = "/var/lib/cao/repo-a/state.sqlite3"
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
sudo install -d -o agent -g agent /var/lib/cao/repo-a /srv/cao/repo-a/worktrees
sudo chown root:agent /etc/cao/repo-a.toml
sudo chmod 0640 /etc/cao/repo-a.toml
```

先以前台单次模式验收，确认 GitHub 和 Agent 登录态、仓库权限及检查命令均正常：

```bash
sudo -u agent /opt/coding-agent-orchestrator/.venv/bin/cao --config /etc/cao/repo-a.toml once
sudo -u agent /opt/coding-agent-orchestrator/.venv/bin/cao --config /etc/cao/repo-a.toml status
```

## 5. Linux：systemd

单仓库可以安装 `coding-agent-orchestrator.service`。多仓库推荐模板服务：

```bash
sudo cp deploy/cao@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cao@repo-a
sudo systemctl enable --now cao@repo-b
```

服务名 `%i` 对应 `/etc/cao/%i.toml`。常用操作：

```bash
systemctl status cao@repo-a
journalctl -u cao@repo-a -f
sudo systemctl restart cao@repo-a
sudo systemctl stop cao@repo-a
```

模板默认只允许写入 `/srv/cao`、`/var/lib/cao` 和 Agent 常见的用户级缓存/配置目录。首次启动前创建实际 Agent CLI 所需的目录；若仓库或 Agent 状态位于其他位置，应精确修改 `ReadWritePaths`，不要放宽为整个根目录或用户主目录。

## 6. macOS：launchd

将项目放到稳定的绝对路径，修改 `com.cao.orchestrator.plist` 中的程序、配置、工作目录和日志路径。每个仓库复制一份 plist，并为 `Label`、配置文件和日志使用唯一名称，例如：

```text
~/Library/LaunchAgents/com.cao.repo-a.plist
~/Library/LaunchAgents/com.cao.repo-b.plist
```

加载并检查：

```bash
mkdir -p /opt/coding-agent-orchestrator/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cao.repo-a.plist
launchctl kickstart -k gui/$(id -u)/com.cao.repo-a
launchctl print gui/$(id -u)/com.cao.repo-a
tail -f /opt/coding-agent-orchestrator/logs/repo-a.stdout.log
```

LaunchAgent 只在用户登录会话中运行。需要开机后无人登录持续运行时，应由管理员创建系统级 LaunchDaemon，明确设置运行用户和最小文件权限，并验证 Agent CLI 的认证机制在无 GUI/钥匙串交互时仍可用。

## 7. 多 Worker 与容量

`runtime.max_workers` 限制单仓库总并发，`agents.<name>.max_workers` 限制特定 Agent 并发，实际并发取二者允许的较小值。一台机器上所有实例的 Worker 总数还应受 CPU、内存、磁盘 I/O 和 API 限额约束。

建议从每仓库 1 个 Worker 开始，观察完整编译和测试的峰值资源后再增加。一般可按每个 Worker 至少 2 个 CPU 核心、2–4 GB 内存估算；大型项目应以实测为准。确保磁盘容量可以容纳主 checkout、并行 worktrees、构建产物和依赖缓存。

## 8. GitHub 与凭据

GitHub 凭据只授予目标仓库所需的 Issues、Contents 和 Pull requests 权限。不要把生产密钥、部署凭据或数据库管理员凭据放入服务环境。建议为常驻机器使用独立机器人账户，并通过以下命令验证身份：

```bash
sudo -u agent gh auth status
sudo -u agent git -C /srv/cao/repo-a/repo fetch origin main
```

如需通过环境变量提供凭据，将其放在 root 可读的凭据管理位置。`/etc/cao/environment` 和 `%i.env` 只适合由权限严格控制的环境文件，不应提交到 Git。

## 9. 升级与回滚

升级前先停止实例，避免在 Worker 执行期间替换代码：

```bash
sudo systemctl stop 'cao@*'
sudo -u agent git -C /opt/coding-agent-orchestrator pull --ff-only
sudo -u agent /opt/coding-agent-orchestrator/.venv/bin/pip install /opt/coding-agent-orchestrator
sudo systemctl start cao@repo-a cao@repo-b
```

安装了 `[dev]` 依赖时，上线前运行项目自身检查：

```bash
cd /opt/coding-agent-orchestrator
.venv/bin/ruff check .
.venv/bin/pytest -q
```

回滚时检出已验证的 Git 提交、重新安装包并重启服务。不要删除 SQLite 或 worktree 来代替状态恢复。

## 10. 备份与恢复

备份范围包括每个仓库的 SQLite 状态文件、配置文件和必要的 Agent CLI 认证资料。仓库代码可从 GitHub 恢复，worktree 通常无需备份。复制 SQLite 前应停止对应实例，或使用 SQLite 在线备份工具生成一致快照。

进程重启时，调度器会把中断状态标记为可重试，并重新读取仍带 `agent-running` 标签的 Issue。恢复后检查：

```bash
cao --config /etc/cao/repo-a.toml status
git -C /srv/cao/repo-a/repo worktree list
gh issue list --repo OWNER/repo-a --label agent-running
```

## 11. 故障排查

- 服务反复重启：查看 journal 或 launchd stderr，检查配置路径和 Python 环境。
- 无法领取 Issue：确认标签名称、GitHub 仓库名和 `gh auth status`。
- 无法创建 worktree：检查 `origin/<base_branch>`、目录权限和残留 worktree。
- Agent 命令找不到：服务不会读取交互式 shell 配置，应在 TOML 中使用绝对路径或显式设置受控 PATH。
- push/PR 失败：验证服务账户的 SSH key 或 GitHub token，以及仓库分支策略。
- 任务长期占用：检查 Agent 超时、目标项目测试超时及机器资源；不要直接启动第二个同仓库实例绕过阻塞。

当前版本不支持多机调度、跨实例锁、自动合并或生产部署。需要高可用多机运行时，应先引入集中式状态库、租约和分布式锁设计。
