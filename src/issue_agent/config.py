from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .codegraph import CodegraphConfig
from .schedule import ScheduleConfig, load_schedule


@dataclass(frozen=True)
class AgentConfig:
    command: tuple[str, ...]
    max_workers: int = 1
    timeout_seconds: int = 3600
    review_command: tuple[str, ...] | None = None
    resume_command: tuple[str, ...] | None = None
    review_resume_command: tuple[str, ...] | None = None


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
    task_checks: tuple[str, ...] | None = None
    check_timeout_seconds: int = 1800
    max_check_workers: int = 1
    baseline_cache_ttl_seconds: int = 300
    baseline_cache_max_entries: int = 32
    checks_parallel: bool = True
    review_task_mode: str = "formal"  # "full" | "formal" | "off"
    codegraph: CodegraphConfig = field(default_factory=CodegraphConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    default_agent: str = "codex"
    reviewer_agent: str = ""
    planner_agent: str = ""
    max_tasks: int = 8
    auto_plan_unlabeled: bool = False
    auto_plan_limit: int = 20
    ready_poll_limit: int = 20
    auto_ready_with_plan: bool = False
    allow_split: bool = False
    max_split_children: int = 5
    max_clarify_rounds: int = 2
    clarify_ignore_authors: tuple[str, ...] = ()
    dry_run: bool = False
    agents: dict[str, AgentConfig] = field(default_factory=dict)


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _parse_bool(key: str, value: object) -> bool:
    """Strict boolean parsing for a config key.

    TOML booleans arrive as bool; a quoted ``"false"`` must fail loudly instead
    of silently meaning True — ``bool("false")`` is truthy, which left an agent
    enabled and ``dry_run`` off when the user meant exactly the opposite.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"{key} must be true or false, got {value!r}")


def _string_list(key: str, value: object) -> tuple[str, ...]:
    """A config key that must be a list of shell command strings.

    A bare ``commands = "pytest -q"`` would ``tuple()`` into single characters,
    each of which would then run as its own shell command.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings, got {value!r}")
    return tuple(value)


def validate_config(config: Config) -> None:
    """Reject invalid execution limits and named Agent references early."""
    positive = {
        "runtime.poll_seconds": config.poll_seconds,
        "runtime.max_workers": config.max_workers,
        "runtime.max_attempts": config.max_attempts,
        "runtime.max_task_attempts": config.max_task_attempts,
        "runtime.max_tasks": config.max_tasks,
        "runtime.auto_plan_limit": config.auto_plan_limit,
        "runtime.ready_poll_limit": config.ready_poll_limit,
        "runtime.max_split_children": config.max_split_children,
        "runtime.max_clarify_rounds": config.max_clarify_rounds,
        "checks.timeout_seconds": config.check_timeout_seconds,
        "checks.max_workers": config.max_check_workers,
        "checks.baseline_cache_max_entries": config.baseline_cache_max_entries,
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
        for command_name, command in (
            ("resume_command", agent.resume_command),
            ("review_resume_command", agent.review_resume_command),
        ):
            if command and not any("{session_id}" in part for part in command):
                raise ValueError(
                    f"agents.{name}.{command_name} must contain {{session_id}}"
                )
    if config.agents and config.default_agent not in config.agents:
        raise ValueError(f"unknown or disabled default_agent: {config.default_agent}")
    if not config.agents:
        # Without agents every ready issue is silently rejected by select_agent
        # forever — the orchestrator would spin with no failure signal at all.
        raise ValueError(
            "no agents configured: define at least one [agents.<name>] with a command"
        )
    for role, name in (
        ("planner_agent", config.planner_agent),
        ("reviewer_agent", config.reviewer_agent),
    ):
        if name and name not in config.agents:
            raise ValueError(f"unknown or disabled {role}: {name}")
    if config.review_task_mode not in {"full", "formal", "off"}:
        raise ValueError(
            f"review.task_mode must be one of full/formal/off, got {config.review_task_mode!r}"
        )


def load_config(path: str | Path) -> Config:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    root = config_path.parent
    runtime = raw.get("runtime", {})
    github = raw.get("github", {})
    checks_section = raw.get("checks", {})
    agents: dict[str, AgentConfig] = {}
    for name, item in raw.get("agents", {}).items():
        if not _parse_bool(f"agents.{name}.enabled", item.get("enabled", True)):
            continue
        agents[name] = AgentConfig(
            command=tuple(shlex.split(item["command"])),
            max_workers=int(item.get("max_workers", 1)),
            timeout_seconds=int(item.get("timeout_seconds", 3600)),
            review_command=(
                tuple(shlex.split(item["review_command"]))
                if item.get("review_command")
                else None
            ),
            resume_command=(
                tuple(shlex.split(item["resume_command"]))
                if item.get("resume_command")
                else None
            ),
            review_resume_command=(
                tuple(shlex.split(item["review_resume_command"]))
                if item.get("review_resume_command")
                else None
            ),
        )

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
        checks=_string_list("checks.commands", checks_section.get("commands", ["pytest -q"])),
        task_checks=(
            _string_list("checks.task_commands", checks_section["task_commands"])
            if "task_commands" in checks_section
            else None
        ),
        check_timeout_seconds=int(checks_section.get("timeout_seconds", 1800)),
        max_check_workers=int(
            checks_section.get("max_workers", runtime.get("max_workers", 3))
        ),
        baseline_cache_ttl_seconds=int(checks_section.get("baseline_cache_ttl_seconds", 300)),
        baseline_cache_max_entries=int(
            checks_section.get("baseline_cache_max_entries", 32)
        ),
        checks_parallel=_parse_bool("checks.parallel", checks_section.get("parallel", True)),
        review_task_mode=str(raw.get("review", {}).get("task_mode", "formal")),
        codegraph=CodegraphConfig(
            enabled=_parse_bool(
                "codegraph.enabled", raw.get("codegraph", {}).get("enabled", True)
            )
        ),
        default_agent=runtime.get("default_agent", "codex"),
        reviewer_agent=runtime.get("reviewer_agent", ""),
        planner_agent=runtime.get("planner_agent", ""),
        max_tasks=int(runtime.get("max_tasks", 8)),
        auto_plan_unlabeled=_parse_bool(
            "runtime.auto_plan_unlabeled", runtime.get("auto_plan_unlabeled", False)
        ),
        auto_plan_limit=int(runtime.get("auto_plan_limit", 20)),
        ready_poll_limit=int(runtime.get("ready_poll_limit", 20)),
        auto_ready_with_plan=_parse_bool(
            "runtime.auto_ready_with_plan", runtime.get("auto_ready_with_plan", False)
        ),
        allow_split=_parse_bool("runtime.allow_split", runtime.get("allow_split", False)),
        max_split_children=int(runtime.get("max_split_children", 5)),
        max_clarify_rounds=int(runtime.get("max_clarify_rounds", 2)),
        # GitHub logins are case-insensitive; normalize once so the comment
        # author comparison does not have to remember that.
        clarify_ignore_authors=tuple(
            str(author).lower() for author in runtime.get("clarify_ignore_authors", [])
        ),
        dry_run=_parse_bool("runtime.dry_run", runtime.get("dry_run", False)),
        agents=agents,
        schedule=load_schedule(raw.get("schedule", {})),
    )
    validate_config(config)
    return config
