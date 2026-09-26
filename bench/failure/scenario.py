"""The workload every system under test replays, and the deterministic fake model.

The fake model answers label(prompt, body): a hash, so the right value of every row is known
from its final body and the final prompt, with no model in the loop.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

LABELS = ["bug", "billing", "account", "question"]
PROMPTS = [
    "Classify the support ticket.",
    "Classify the support ticket by its root cause.",
]
WORDS = (
    "checkout invoice login password refund export csv error slow crash charge plan card "
    "email reset account api timeout order duplicate report dashboard mobile sync"
).split()


def label(prompt: str, body: str) -> str:
    digest = hashlib.sha256(f"{prompt}\x00{body}".encode()).digest()
    return LABELS[digest[0] % len(LABELS)]


def user_message(prompt: str, body: str) -> str:
    """The text the model receives, in the same shape as pgbee.providers.user_message."""
    return f"{prompt}\n\nData:\n{body}"


def parse_user_message(content: str) -> tuple[str, str]:
    prompt, _, body = content.partition("\n\nData:\n")
    return prompt, body


@dataclass(frozen=True)
class Op:
    seq: int
    kind: str  # insert, edit (through the app), script_edit (bypasses the app), fix, prompt
    row_id: int = 0
    body: str = ""
    value: str = ""  # the human value of a fix
    prompt: str = ""


def build(rows: int, seed: int) -> list[Op]:
    """Inserts, edits soon after insert (the race window), late edits, one prompt change,
    human fixes on rows that are not edited afterwards."""
    rng = random.Random(seed)
    timeline: list[tuple[float, dict[str, object]]] = []
    revision: dict[int, int] = {}
    last_edit: dict[int, float] = {}
    bodies_of: dict[int, list[str]] = {}

    def body(row: int) -> str:
        revision[row] = revision.get(row, -1) + 1
        text = " ".join(rng.choice(WORDS) for _ in range(8))
        bodies_of.setdefault(row, []).append(f"#{row} r{revision[row]} {text}")
        return bodies_of[row][-1]

    edits: list[tuple[float, int]] = []
    for row in range(1, rows + 1):
        timeline.append((float(row), {"kind": "insert", "row_id": row}))
        last_edit[row] = float(row)
        if rng.random() < 0.25:
            edits.append((row + rng.uniform(0.2, 4.0), row))
        if rng.random() < 0.10:
            edits.append((rng.uniform(row + 10, rows + 10), row))
    for at, row in sorted(edits):
        kind = "edit" if rng.random() < 0.7 else "script_edit"
        timeline.append((at, {"kind": kind, "row_id": row}))
        last_edit[row] = max(last_edit[row], at)
    timeline.append((rows * 0.55, {"kind": "prompt", "prompt": PROMPTS[1]}))

    # Fixes go on rows whose text is final by then. Their bodies are not known yet (they are
    # drawn in order below), so the value is chosen when the op is built.
    fixed = rng.sample(range(1, rows // 2), k=max(1, rows * 3 // 100))
    for row in fixed:
        at = rng.uniform(last_edit[row] + 20, rows + 30)
        last_edit[row] = at
        timeline.append((at, {"kind": "fix", "row_id": row}))

    timeline.sort(key=lambda item: item[0])
    ops: list[Op] = []
    for _, spec in timeline:
        seq = len(ops) + 1
        kind = str(spec["kind"])
        row = int(spec.get("row_id", 0))  # type: ignore[call-overload]
        if kind in ("insert", "edit", "script_edit"):
            ops.append(Op(seq, kind, row, body=body(row)))
        elif kind == "fix":
            # A real correction: a value the model gives for none of the row's texts under
            # either prompt, so the write always changes the column. Writing the value a column
            # already holds is not an override in pgbee (ORMs rewrite every column on save).
            model_values = {label(p, b) for p in PROMPTS for b in bodies_of[row]}
            choices = [v for v in LABELS if v not in model_values]
            if choices:
                ops.append(Op(seq, kind, row, value=rng.choice(choices)))
        else:
            ops.append(Op(seq, kind, prompt=str(spec["prompt"])))
    return ops


def expected(ops: list[Op]) -> dict[int, str]:
    """The right value of every row once the system has settled."""
    bodies: dict[int, str] = {}
    fixes: dict[int, str] = {}
    prompt = PROMPTS[0]
    for op in ops:
        if op.kind in ("insert", "edit", "script_edit"):
            bodies[op.row_id] = op.body
        elif op.kind == "fix":
            fixes[op.row_id] = op.value
        else:
            prompt = op.prompt
    return {row: fixes.get(row, label(prompt, body)) for row, body in bodies.items()}


def save(ops: list[Op], path: Path) -> None:
    path.write_text(json.dumps([asdict(op) for op in ops]))


def load(path: Path) -> list[Op]:
    return [Op(**item) for item in json.loads(path.read_text())]
