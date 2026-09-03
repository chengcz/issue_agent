from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .codegraph import CodegraphConfig


@dataclass(frozen=True)
class AgentConfig:
    command: tuple[str, ...]
    max_workers: int = 1
    timeout_seconds: int = 3600
    review_command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Config:
    repo: Path
    worktrees: Path
    state_db: Path
    log_dir: Path
    github_repo: str
    base_branch: str = "main"
    ready_label: str = "agent-ready"
    poll_seconds: int = 60
    fetch_ttl_seconds: int = 30
    max_workers: int = 3
    max_attempts: int = 3
    max_task_attempts: int = 2
    checks: tuple[str, ...] = ("pytest -q",)
    check_timeout_seconds: int = 1800
    baseline_cache_ttl_seconds: int = 300
    checks_parallel: bool = True
    codegraph: CodegraphConfig = field(default_factory=CodegraphConfig)
    default_agent: str = "codex"
    reviewer_agent: str = ""
    planner_agent: str = ""
    max_tasks: int = 8
    auto_plan_unlabeled: bool = False
    auto_plan_limit: int = 20
    dry_run: bool = False
    agents: dict[str, AgentConfig] = field(default_factory=dict)


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def validate_config(config: Config) -> None:
    """Reject invalid execution limits and named Agent references early."""
    positive = {
        "runtime.poll_seconds": config.poll_seconds,
        "runtime.max_workers": config.max_workers,
        "runtime.max_attempts": config.max_attempts,
        "runtime.max_task_attempts": config.max_task_attempts,
        "runtime.max_tasks": config.max_tasks,
        "runtime.auto_plan_limit": config.auto_plan_limit,
        "checks.timeout_seconds": config.check_timeout_seconds,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")
    if config.fetch_ttl_seconds < 0:
        raise ValueError("runtime.fetch_ttl_seconds must not be negative")
    if config.baseline_cache_ttl_seconds < 0:
        raise ValueError("checks.baseline_cache_ttl_seconds must not be negative")
    for name, agent in config.agents.items():
        if not agent.command:
            raise ValueError(f"agents.{name}.command must not be empty")
        if agent.max_workers <= 0:
            raise ValueError(f"agents.{name}.max_workers must be greater than zero")
        if agent.timeout_seconds <= 0:
            raise ValueError(f"agents.{name}.timeout_seconds must be greater than zero")
    if config.agents and config.default_agent not in config.agents:
        raise ValueError(f"unknown or disabled default_agent: {config.default_agent}")
    for role, name in (
        ("planner_agent", config.planner_agent),
        ("reviewer_agent", config.reviewer_agent),
    ):
        if name and name not in config.agents:
            raise ValueError(f"unknown or disabled {role}: {name}")


def load_config(path: str | Path) -> Config:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    root = config_path.parent
    runtime = raw.get("runtime", {})
    github = raw.get("github", {})
    agents = {
        name: AgentConfig(
            command=tuple(shlex.split(item["command"])),
            max_workers=int(item.get("max_workers", 1)),
            timeout_seconds=int(item.get("timeout_seconds", 3600)),
            review_command=(
                tuple(shlex.split(item["review_command"]))
                if item.get("review_command")
                else None
            ),
        )
        for name, item in raw.get("agents", {}).items()
        if item.get("enabled", True)
    }

    def resolve(value: str) -> Path:
        candidate = Path(_expand(value))
        return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()

    config = Config(
        repo=resolve(runtime.get("repo", ".")),
        worktrees=resolve(runtime.get("worktrees", "issue-agent/worktrees")),
        state_db=resolve(runtime.get("state_db", "issue-agent/state.sqlite3")),
        log_dir=resolve(runtime.get("log_dir", "issue-agent/logs")),
        github_repo=github.get("repo", ""),
        base_branch=github.get("base_branch", "main"),
        ready_label=github.get("ready_label", "agent-ready"),
        poll_seconds=int(runtime.get("poll_seconds", 60)),
        fetch_ttl_seconds=int(runtime.get("fetch_ttl_seconds", 30)),
        max_workers=int(runtime.get("max_workers", 3)),
        max_attempts=int(runtime.get("max_attempts", 3)),
        max_task_attempts=int(runtime.get("max_task_attempts", 2)),
        checks=tuple(raw.get("checks", {}).get("commands", ["pytest -q"])),
        check_timeout_seconds=int(raw.get("checks", {}).get("timeout_seconds", 1800)),
        baseline_cache_ttl_seconds=int(raw.get("checks", {}).get("baseline_cache_ttl_seconds", 300)),
        checks_parallel=bool(raw.get("checks", {}).get("parallel", True)),
        codegraph=CodegraphConfig(
            enabled=bool(raw.get("codegraph", {}).get("enabled", True))
        ),
        default_agent=runtime.get("default_agent", "codex"),
        reviewer_agent=runtime.get("reviewer_agent", ""),
        planner_agent=runtime.get("planner_agent", ""),
        max_tasks=int(runtime.get("max_tasks", 8)),
        auto_plan_unlabeled=bool(runtime.get("auto_plan_unlabeled", False)),
        auto_plan_limit=int(runtime.get("auto_plan_limit", 20)),
        dry_run=bool(runtime.get("dry_run", False)),
        agents=agents,
    )
    validate_config(config)
    return config
