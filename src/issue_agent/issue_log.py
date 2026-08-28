from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IssueLog:
    """Append structured execution and review records for one GitHub Issue."""

    root: Path
    issue_number: int

    @property
    def execution_path(self) -> Path:
        return self.root / f"issue-{self.issue_number}.jsonl"

    @property
    def review_path(self) -> Path:
        return self.root / f"issue-{self.issue_number}.reviews.jsonl"

    def event(self, name: str, **data: Any) -> None:
        self._append(self.execution_path, name, data)

    def review(self, phase: str, output: str, **data: Any) -> None:
        self._append(self.review_path, "review", {"phase": phase, "output": output, **data})

    def _append(self, path: Path, name: str, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "issue_number": self.issue_number,
            "event": name,
            **data,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
