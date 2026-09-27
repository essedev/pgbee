"""Failure injection: pgbee against two app-side designs, on the same workload and the same faults.

Each system gets its own database, its own application process and two worker processes. The
driver replays the workload (scenario.py), kills processes with SIGKILL at random moments (the
same victim in every system at the same time), takes the external queue of the naive design down
twice, and lets everything settle. Then it compares every row with its right value.

Nothing is paid: the model is a local fake server that answers a hash of prompt and text, with
random latency and injected 5xx errors. pgbee's worker is the real `pgbee run` process, pointed
at the fake server through OPENROUTER_BASE_URL.

Run from the worker directory, with the demo Postgres up (make db-up):
    uv run python ../bench/failure/chaos.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
from scenario import LABELS, PROMPTS, Op, build, expected, label, parse_user_message, save

from pgbee.installer import install

HERE = Path(__file__).resolve().parent
SYSTEMS = ["pgbee", "naive", "outbox"]
WORKERS_PER_SYSTEM = 2


class FakeModel:
    """OpenAI-shaped chat completions: the value is label(prompt, text), after 20-300 ms."""

    def __init__(self, error_rate: float, seed: int) -> None:
        self.calls: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._error_rate = error_rate
        model = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                status, payload = model.answer(request)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def answer(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        model = str(request["model"])
        with self._lock:
            delay = self._rng.uniform(0.02, 0.3)
            fail = self._rng.random() < self._error_rate
            self.calls[model] += 1
            if fail:
                self.errors[model] += 1
        time.sleep(delay)
        if fail:
            return 500, {"error": {"message": "injected failure", "code": 500}}
        prompt, text = parse_user_message(str(request["messages"][-1]["content"]))
        content = json.dumps({"value": label(prompt, text), "confidence": 0.9})
        return 200, {
            "id": "fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58},
        }

    def stop(self) -> None:
        self._server.shutdown()


def db_url(admin_url: str, name: str) -> str:
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{name}"


def setup(admin_url: str) -> dict[str, str]:
    urls = {name: db_url(admin_url, f"bench_{name}") for name in [*SYSTEMS, "queue"]}
    with psycopg.connect(admin_url, autocommit=True) as admin:
        for url in urls.values():
            name = url.rpartition("/")[2]
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"CREATE DATABASE {name}")
    queue_table = (
        "CREATE TABLE job (id bigserial PRIMARY KEY, row_id int NOT NULL,"
        " locked_until timestamptz NOT NULL DEFAULT '-infinity')"
    )
    progress = (
        "CREATE TABLE bench_progress (seq int NOT NULL); INSERT INTO bench_progress VALUES (0)"
    )
    with psycopg.connect(urls["pgbee"]) as conn:
        install(conn)
        conn.commit()  # new enum values must be committed before add_column uses them
        conn.execute("CREATE TABLE ticket (id int PRIMARY KEY, body text NOT NULL)")
        conn.execute(progress)
        conn.execute(
            "SELECT bee.add_column('ticket', 'category', array['body'], 'enum',"
            " p_prompt => %s, p_model => 'fake/pgbee', p_output_schema => %s::jsonb,"
            " p_config => %s::jsonb)",
            (
                PROMPTS[0],
                json.dumps(LABELS),
                json.dumps({"backoff_base_seconds": 1, "max_attempts": 10}),
            ),
        )
    for system in ("naive", "outbox"):
        with psycopg.connect(urls[system]) as conn:
            conn.execute(
                "CREATE TABLE ticket (id int PRIMARY KEY, body text NOT NULL, category text)"
            )
            conn.execute("CREATE TABLE bench_config (prompt text NOT NULL)")
            conn.execute("INSERT INTO bench_config VALUES (%s)", (PROMPTS[0],))
            conn.execute(progress)
            if system == "outbox":
                conn.execute(queue_table)
    with psycopg.connect(urls["queue"]) as conn:
        conn.execute(queue_table)
    return urls


@dataclass
class Proc:
    system: str
    role: str
    index: int
    argv: list[str]
    env: dict[str, str]
    log: Path
    popen: subprocess.Popen[bytes] | None = None
    finished: bool = False
    respawn_at: float | None = None
    kills: int = 0
    unexpected_exits: list[int] = field(default_factory=list)

    def start(self) -> None:
        with self.log.open("ab") as out:
            self.popen = subprocess.Popen(
                self.argv, env=self.env, cwd=HERE, stdout=out, stderr=subprocess.STDOUT
            )
        self.respawn_at = None

    def kill(self, respawn_at: float) -> None:
        if self.popen is not None and self.popen.poll() is None:
            self.popen.send_signal(signal.SIGKILL)
            self.popen.wait()
            self.kills += 1
        self.respawn_at = respawn_at

    def stop(self) -> None:
        if self.popen is not None and self.popen.poll() is None:
            self.popen.terminate()
            try:
                self.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.popen.kill()


def processes(
    urls: dict[str, str],
    ops_path: Path,
    flag: Path,
    port: int,
    args: argparse.Namespace,
    logs: Path,
) -> list[Proc]:
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENROUTER_", "PGBEE_"))}
    actors = [sys.executable, str(HERE / "actors.py")]
    procs: list[Proc] = []
    for system in SYSTEMS:
        common = ["--system", system, "--db", urls[system], "--queue-db", urls["queue"]]
        common += ["--outage-flag", str(flag)]
        app = [*actors, "app", *common, "--ops", str(ops_path), "--pace", str(args.pace)]
        procs.append(Proc(system, "app", 0, app, base_env, logs / f"{system}-app.log"))
        for i in range(WORKERS_PER_SYSTEM):
            if system == "pgbee":
                argv = [str(Path(sys.executable).with_name("pgbee")), "run", "--backends", "llm"]
                env = {
                    **base_env,
                    "DATABASE_URL": urls[system],
                    "OPENROUTER_API_KEY": "fake-key-no-network",
                    "OPENROUTER_BASE_URL": f"http://127.0.0.1:{port}/api/v1",
                    "PGBEE_WORKER_ID": f"bench-{i}",
                    "PGBEE_POLL_INTERVAL_SECONDS": "1",
                    "PGBEE_CLAIM_TIMEOUT_SECONDS": str(args.lease),
                    "PGBEE_LOG_LEVEL": "warning",
                }
            else:
                model_url = f"http://127.0.0.1:{port}/v1/chat/completions"
                argv = [*actors, "worker", *common, "--model-url", model_url]
                argv += ["--lease", str(args.lease)]
                env = base_env
            procs.append(Proc(system, "worker", i, argv, env, logs / f"{system}-worker{i}.log"))
    return procs


def backlog(urls: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    with psycopg.connect(urls["pgbee"]) as conn:
        row = conn.execute(
            "SELECT (SELECT count(*) FROM bee.job WHERE status IN ('pending', 'claimed'))"
            " + (SELECT count(*) FROM bee.column_def WHERE backfill_pending)"
        ).fetchone()
        out["pgbee"] = int(row[0]) if row else 0
    for system, url in (("naive", urls["queue"]), ("outbox", urls["outbox"])):
        with psycopg.connect(url) as conn:
            row = conn.execute("SELECT count(*) FROM job").fetchone()
            out[system] = int(row[0]) if row else 0
    return out


def run_chaos(
    procs: list[Proc], urls: dict[str, str], flag: Path, args: argparse.Namespace, n_ops: int
) -> dict[str, Any]:
    """Replay the workload with kills every `kill_every` operations (on average), and the naive
    queue down when its application reaches 30% and 70% of the operations. Both follow the
    progress of the naive application, so the faults do not depend on the machine's speed."""
    rng = random.Random(args.seed)
    for p in procs:
        p.start()
    started = time.monotonic()
    marks = [int(n_ops * at) for at in (0.3, 0.7)][: args.outages]
    outages: list[tuple[float, float]] = []
    next_kill = args.kill_every
    kills: list[dict[str, Any]] = []
    with psycopg.connect(urls["naive"], autocommit=True) as naive:
        while not all(p.finished for p in procs if p.role == "app"):
            now = time.monotonic()
            supervise(procs, now)
            row = naive.execute("SELECT seq FROM bench_progress").fetchone()
            progress = row[0] if row else 0
            if marks and progress >= marks[0]:
                marks.pop(0)
                outages.append((now, now + args.outage_seconds))
            down = any(a <= now < b for a, b in outages)
            if down and not flag.exists():
                flag.touch()
            elif not down and flag.exists():
                flag.unlink()
            if progress >= next_kill:
                role = "app" if rng.random() < 0.4 else "worker"
                index = 0 if role == "app" else rng.randrange(WORKERS_PER_SYSTEM)
                respawn = now + rng.uniform(0.5, 1.5)
                for p in procs:
                    if p.role == role and p.index == index and not p.finished:
                        p.kill(respawn)
                kills.append({"at_op": progress, "role": role, "index": index})
                next_kill = progress + args.kill_every * rng.uniform(0.5, 1.5)
            time.sleep(0.05)
    flag.unlink(missing_ok=True)
    return {
        "load_seconds": round(time.monotonic() - started, 1),
        "kills": kills,
        "queue_outages_s": [(round(a - started, 1), round(b - started, 1)) for a, b in outages],
    }


def supervise(procs: list[Proc], now: float) -> None:
    for p in procs:
        if p.finished or p.popen is None:
            continue
        if p.respawn_at is not None:
            if now >= p.respawn_at:
                p.start()
            continue
        rc = p.popen.poll()
        if rc is None:
            continue
        if p.role == "app" and rc == 0:
            p.finished = True
            continue
        p.unexpected_exits.append(rc)
        p.respawn_at = now + 0.5


def drain(procs: list[Proc], urls: dict[str, str], timeout: float) -> dict[str, float | None]:
    """Keep workers alive until each system's queue stays empty for three seconds."""
    started = time.monotonic()
    settled: dict[str, float | None] = dict.fromkeys(SYSTEMS)
    quiet_since: dict[str, float | None] = dict.fromkeys(SYSTEMS)
    while time.monotonic() - started < timeout and None in settled.values():
        now = time.monotonic()
        supervise(procs, now)
        for system, n in backlog(urls).items():
            if settled[system] is not None:
                continue
            if n:
                quiet_since[system] = None
                continue
            since = quiet_since[system] or now
            quiet_since[system] = since
            if now - since >= 3:
                settled[system] = round(since - started, 1)
        time.sleep(0.5)
    return settled


def measure(urls: dict[str, str], ops: list[Op], fake: FakeModel) -> dict[str, dict[str, Any]]:
    right = expected(ops)
    fixes = {op.row_id: op.value for op in ops if op.kind == "fix"}
    bodies: dict[int, list[str]] = {}
    for op in ops:
        if op.body:
            bodies.setdefault(op.row_id, []).append(op.body)
    out: dict[str, dict[str, Any]] = {}
    for system in SYSTEMS:
        with psycopg.connect(urls[system]) as conn:
            rows = conn.execute("SELECT id, body, category FROM ticket").fetchall()
            pgbee_view: dict[str, int] = {}
            if system == "pgbee":
                for name, sql in {
                    "flagged_stale": "SELECT count(*) FROM bee.stale_rows",
                    "dead_jobs": "SELECT count(*) FROM bee.dead_jobs",
                    "human_overrides": "SELECT count(*) FROM bee.result"
                    " WHERE is_current AND source = 'human'",
                }.items():
                    row = conn.execute(sql).fetchone()
                    pgbee_view[name] = int(row[0]) if row else 0
        stats: Counter[str] = Counter()
        for row_id, body, value in rows:
            if body != bodies[row_id][-1]:
                stats["body_not_replayed"] += 1
            if row_id in fixes:
                if value != fixes[row_id]:
                    stats["fixes_overwritten"] += 1
                continue
            if value is None:
                stats["missing"] += 1
            elif value != right[row_id]:
                stats["wrong"] += 1
                if value == label(PROMPTS[0], body):
                    stats["wrong_old_prompt"] += 1
                elif any(value == label(p, b) for p in PROMPTS for b in bodies[row_id][:-1]):
                    stats["wrong_old_text"] += 1
            else:
                stats["right"] += 1
        model = f"fake/{system}"
        out[system] = {
            "rows": len(rows),
            "right": stats["right"],
            "missing": stats["missing"],
            "wrong": stats["wrong"],
            "wrong_old_text": stats["wrong_old_text"],
            "wrong_old_prompt": stats["wrong_old_prompt"],
            "fixes": len(fixes),
            "fixes_overwritten": stats["fixes_overwritten"],
            "body_not_replayed": stats["body_not_replayed"],
            "model_calls": fake.calls[model],
            "model_errors": fake.errors[model],
            **pgbee_view,
        }
    return out


def report(results: dict[str, Any]) -> None:
    systems = results["systems"]
    lines = [
        ("rows", "rows"),
        ("missing", "no value"),
        ("wrong", "stale value, looks fresh"),
        ("wrong_old_text", "  computed on an older text"),
        ("wrong_old_prompt", "  computed with the old prompt"),
        ("fixes_overwritten", "human fixes overwritten"),
        ("model_calls", "model calls"),
    ]
    print(f"\n{'':32}" + "".join(f"{s:>10}" for s in SYSTEMS))
    for key, name in lines:
        print(f"{name:32}" + "".join(f"{systems[s][key]:>10}" for s in SYSTEMS))
    print(
        f"{'settled after load (s)':32}"
        + "".join(f"{results['settled_s'][s]!s:>10}" for s in SYSTEMS)
    )
    print(f"\nkills: {len(results['kills'])}, human fixes: {systems['pgbee']['fixes']}")
    pg = systems["pgbee"]
    print(
        f"pgbee's own view: {pg['flagged_stale']} stale rows, {pg['dead_jobs']} dead jobs,"
        f" {pg['human_overrides']} human overrides"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--pace", type=float, default=0.015, help="seconds between app operations")
    parser.add_argument("--kill-every", type=int, default=60, help="mean operations between kills")
    parser.add_argument("--outages", type=int, default=2, help="outages of the naive queue")
    parser.add_argument("--outage-seconds", type=float, default=4.0)
    parser.add_argument("--error-rate", type=float, default=0.03, help="injected model 5xx")
    parser.add_argument("--lease", type=int, default=10, help="claim timeout and queue lease")
    parser.add_argument("--timeout", type=float, default=600.0, help="max seconds to settle")
    parser.add_argument("--out", type=Path, help="default: bench/results/failure-seed<seed>.json")
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("DATABASE_URL", "postgresql://aidb:aidb@localhost:4460/aidb"),
    )
    args = parser.parse_args()
    if args.out is None:
        args.out = HERE.parent / "results" / f"failure-seed{args.seed}.json"

    ops = build(args.rows, args.seed)
    work = Path(tempfile.mkdtemp(prefix="pgbee-chaos-"))
    ops_path = work / "ops.json"
    save(ops, ops_path)
    flag = work / "queue-down"
    print(f"{len(ops)} operations on {args.rows} rows, logs in {work}")

    urls = setup(args.admin_url)
    fake = FakeModel(args.error_rate, args.seed)
    procs = processes(urls, ops_path, flag, fake.port, args, work)
    try:
        chaos = run_chaos(procs, urls, flag, args, len(ops))
        print(f"load done in {chaos['load_seconds']}s, {len(chaos['kills'])} kills; settling")
        settled = drain(procs, urls, args.timeout)
    finally:
        for p in procs:
            p.stop()
        fake.stop()
    results = {
        "params": {
            # Paths relative to the repository: results are committed.
            k: os.path.relpath(v, HERE.parents[1]) if isinstance(v, Path) else v
            for k, v in vars(args).items()
            if k != "admin_url"
        },
        "operations": Counter(op.kind for op in ops),
        **chaos,
        "settled_s": settled,
        "unexpected_exits": {
            f"{p.system}-{p.role}{p.index}": p.unexpected_exits for p in procs if p.unexpected_exits
        },
        "systems": measure(urls, ops, fake),
    }
    args.out.write_text(json.dumps(results, indent=2) + "\n")
    report(results)


if __name__ == "__main__":
    main()
