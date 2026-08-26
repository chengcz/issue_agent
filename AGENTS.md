# Repository Instructions

## Scope

This repository implements a provider-neutral coding agent orchestrator.

## Rules

- Python 3.11+ and standard library first.
- Provider-specific behavior belongs in adapters/configuration, not scheduler logic.
- Agents may edit only their worktree. Git push, PR creation, merge, deployment, secrets,
  and production operations must remain outside the coding-agent prompt.
- Never add automatic merge or production deployment without explicit human approval design.
- Persist task transitions before externally visible GitHub transitions where practical.
- Add tests for routing, state recovery, command construction, and retry behavior.
- Run `ruff check .` and `pytest -q` before completing changes.

