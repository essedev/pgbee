"""Job payloads as returned by ai.claim_jobs, and the text the models see."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Job:
    job_id: int
    column_def_id: int
    column_version_id: int
    backend: str
    model: str
    prompt: str | None
    output_type: str
    output_schema: Any
    backend_config: dict[str, Any]
    config: dict[str, Any]
    row_pk: dict[str, Any]
    source_hash: bytes
    source: dict[str, Any]
    attempts: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Job:
        return cls(
            job_id=int(row["job_id"]),
            column_def_id=int(row["column_def_id"]),
            column_version_id=int(row["column_version_id"]),
            backend=str(row["backend"]),
            model=str(row["model"]),
            prompt=row["prompt"],
            output_type=str(row["output_type"]),
            output_schema=row["output_schema"],
            backend_config=dict(row["backend_config"] or {}),
            config=dict(row["config"] or {}),
            row_pk=dict(row["row_pk"]),
            source_hash=bytes(row["source_hash"]),
            source=dict(row["source"]),
            attempts=int(row["attempts"]),
        )

    def source_text(self) -> str:
        """Render the sources for a model: raw value for one column, labeled block otherwise."""
        if len(self.source) == 1:
            return _as_text(next(iter(self.source.values())))
        return "\n".join(f"{name}: {_as_text(value)}" for name, value in self.source.items())


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
