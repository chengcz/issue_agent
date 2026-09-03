"""CodeGraph integration (form B): probe the main-checkout index and build prompt guidance.

The orchestrator never spawns codegraph subprocesses and never writes the index;
MCP setup is the user's responsibility (``codegraph install``), and the index is
user-maintained (``codegraph init`` / ``codegraph watch``). When the index is
missing or the feature is disabled, :func:`guidance_block` returns an empty
string so prompts stay byte-identical to the pre-codegraph behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodegraphConfig:
    """[codegraph] configuration section."""

    enabled: bool = True


def index_ready(repo: Path) -> bool:
    """Whether the target repo's main checkout carries a .codegraph/ index."""
    try:
        return (repo / ".codegraph").is_dir()
    except OSError:
        return False


def guidance_block(repo: Path, cfg: CodegraphConfig) -> str:
    """Prompt guidance appended when the index is ready; '' when unavailable."""
    if not cfg.enabled or not index_ready(repo):
        return ""
    return (
        "## Code intelligence (codegraph)\n"
        f"A codegraph index exists at {repo}/.codegraph (main checkout; this worktree shares it).\n"
        "Prefer the codegraph MCP tools (codegraph_context, codegraph_search, codegraph_callers,\n"
        "codegraph_impact) to explore code structure instead of grep -r or reading whole files.\n"
        "If MCP tools are unavailable, use the read-only CLI:\n"
        f'  codegraph query "<symbol>" --path {repo} --json\n'
        f'  codegraph context "<question>" --path {repo}\n'
        "Never run codegraph init/index/sync - the index is user-maintained."
    )
