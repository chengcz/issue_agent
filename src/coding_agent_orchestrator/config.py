from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    github_repo: str
    base_branch: str = "main"
    ready_label: str = "agent-ready"
    poll_seconds: int = 60
    max_workers: int = 3
    max_attempts: int = 3
    checks: tuple[str, ...] = ("pytest -q",)
    default_agent: str = "codex"
    reviewer_agent: str = ""
    dry_run: bool = False
    agents: dict[str, AgentConfig] = field(default_factory=dict)


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


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

    return Config(
        repo=resolve(runtime.get("repo", ".")),
        worktrees=resolve(runtime.get("worktrees", "autocode/worktrees")),
        state_db=resolve(runtime.get("state_db", "autocode/state.sqlite3")),
        github_repo=github.get("repo", ""),
        base_branch=github.get("base_branch", "main"),
        ready_label=github.get("ready_label", "agent-ready"),
        poll_seconds=int(runtime.get("poll_seconds", 60)),
        max_workers=int(runtime.get("max_workers", 3)),
        max_attempts=int(runtime.get("max_attempts", 3)),
        checks=tuple(raw.get("checks", {}).get("commands", ["pytest -q"])),
        default_agent=runtime.get("default_agent", "codex"),
        reviewer_agent=runtime.get("reviewer_agent", ""),
        dry_run=bool(runtime.get("dry_run", False)),
        agents=agents,
    )
