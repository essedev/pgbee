"""The OpenAI-compatible provider against a local fake endpoint (labeled as such), provider
selection from the environment, and token pricing."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
import pytest
from fakes import add_urgency
from psycopg.rows import DictRow

from pgbee.db import Contract
from pgbee.jobs import Job
from pgbee.providers import OpenAICompatibleProvider, OpenRouterProvider, ProviderError
from pgbee.settings import Settings
from pgbee.worker import Worker, priced


class FakeEndpoint:
    """A local OpenAI-compatible server: records each request body, answers "high" with fixed
    token counts and no cost, like OpenAI or Ollama."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                endpoint.requests.append({"path": self.path, **body})
                if self.path.endswith("/embeddings"):
                    payload: dict[str, Any] = {
                        "object": "list",
                        "model": body["model"],
                        "data": [
                            {"object": "embedding", "index": i, "embedding": [0.5, 0.5, 0.0]}
                            for i, _ in enumerate(body["input"])
                        ],
                        "usage": {"prompt_tokens": 10 * len(body["input"]), "total_tokens": 0},
                    }
                else:
                    content = json.dumps({"value": "high", "confidence": 0.9})
                    payload = {
                        "id": "fake",
                        "object": "chat.completion",
                        "created": 0,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 200,
                            "total_tokens": 1200,
                        },
                    }
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def endpoint() -> Iterator[FakeEndpoint]:
    fake = FakeEndpoint()
    yield fake
    fake.server.shutdown()


def job(backend_config: dict[str, Any] | None = None) -> Job:
    return Job(
        job_id=1,
        column_def_id=1,
        column_version_id=1,
        backend="llm",
        model="local/model",
        prompt="Classify the urgency",
        output_type="enum",
        output_schema=["low", "medium", "high"],
        backend_config=backend_config or {},
        config={},
        row_pk={"id": 1},
        source_hash=b"x",
        source={"body": "the site is down"},
        attempts=1,
    )


async def test_generic_provider_sends_only_standard_parameters(endpoint: FakeEndpoint) -> None:
    provider = OpenAICompatibleProvider("not-needed", endpoint.url)
    config = {"temperature": 0, "max_tokens": 50, "reasoning": {"effort": "low"}, "top_p": 1}
    result = await provider.derive(job(config))
    assert result.value == "high" and result.confidence == pytest.approx(0.9)
    assert result.usage == {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
    request = endpoint.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["max_tokens"] == 50 and request["reasoning_effort"] == "low"
    assert request["temperature"] == 0 and request["top_p"] == 1
    assert request["response_format"]["type"] == "json_schema"
    assert "usage" not in request and "reasoning" not in request


async def test_generic_provider_rejects_openrouter_only_options(endpoint: FakeEndpoint) -> None:
    provider = OpenAICompatibleProvider("not-needed", endpoint.url)
    with pytest.raises(ProviderError, match="needs OpenRouter") as exc:
        await provider.derive(job({"reasoning": {"enabled": False}}))
    assert exc.value.retryable is False
    with pytest.raises(ProviderError, match="decision backend needs OpenRouter") as exc:
        await provider.decide([job()])
    assert exc.value.retryable is False
    assert endpoint.requests == []


async def test_generic_provider_embeds(endpoint: FakeEndpoint) -> None:
    provider = OpenAICompatibleProvider("not-needed", endpoint.url)
    result = await provider.embed("local/embed", ["a", "b"], {"dimensions": 3})
    assert result.vectors == [[0.5, 0.5, 0.0], [0.5, 0.5, 0.0]]
    assert endpoint.requests[0]["dimensions"] == 3


def test_openrouter_keeps_its_reasoning_and_usage_options() -> None:
    provider = OpenRouterProvider("key", "https://openrouter.ai/api/v1")
    params, extra = provider.chat_options(job({"max_tokens": 50, "reasoning": {"enabled": False}}))
    assert params == {"max_completion_tokens": 50}
    assert extra == {"usage": {"include": True}, "reasoning": {"enabled": False}}
    assert provider.backends == ("llm", "decision", "embedding")


def test_priced_fills_cost_from_token_prices_only_when_missing() -> None:
    config = {"input_usd_per_mtok": 0.5, "output_usd_per_mtok": 2}
    tokens = {"prompt_tokens": 1000, "completion_tokens": 200}
    assert priced(tokens, config)["cost"] == pytest.approx(0.0009)
    assert priced({**tokens, "cost": 0.1}, config)["cost"] == 0.1
    assert priced(tokens, {}) == tokens
    assert priced({"prompt_tokens": 30}, {"input_usd_per_mtok": 0.02})["cost"] == pytest.approx(
        6e-7
    )


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "PGBEE_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_provider_is_chosen_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings(monkeypatch, OPENROUTER_API_KEY="k").provider_kind() == "openrouter"
    assert settings(monkeypatch, OPENAI_API_KEY="k").provider_kind() == "openai"
    local = settings(monkeypatch, OPENAI_BASE_URL="http://localhost:11434/v1")
    assert local.provider_kind() == "openai"
    both = settings(monkeypatch, OPENROUTER_API_KEY="k", OPENAI_API_KEY="k")
    assert both.provider_kind() == "openrouter"
    forced = settings(
        monkeypatch, OPENROUTER_API_KEY="k", OPENAI_API_KEY="k", PGBEE_PROVIDER="openai"
    )
    assert forced.provider_kind() == "openai"
    with pytest.raises(ValueError, match="no model provider"):
        settings(monkeypatch).provider_kind()
    with pytest.raises(ValueError, match="needs OPENROUTER_API_KEY"):
        settings(monkeypatch, OPENAI_API_KEY="k", PGBEE_PROVIDER="openrouter").provider_kind()


def test_token_prices_are_validated(conn: psycopg.Connection[DictRow], ticket: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="input_usd_per_mtok"):
        add_urgency(conn, input_usd_per_mtok="0.5")
    with pytest.raises(psycopg.errors.RaiseException, match="output_usd_per_mtok"):
        add_urgency(conn, output_usd_per_mtok=-1)
    add_urgency(conn, input_usd_per_mtok=0.5, output_usd_per_mtok=None)


async def test_worker_prices_tokens_for_spend_and_budget(
    conn: psycopg.Connection[DictRow], ticket: str, database_url: str, endpoint: FakeEndpoint
) -> None:
    # 1000 input and 200 output tokens per row: 0.0009 USD. The cap is reached by the second row.
    add_urgency(conn, input_usd_per_mtok=0.5, output_usd_per_mtok=2, budget_usd=0.0015)
    contract = await Contract.connect(database_url)
    try:
        provider = OpenAICompatibleProvider("not-needed", endpoint.url)
        worker = Worker(contract, provider, worker_id="test", batch_size=1, backends=["llm"])
        batches = [await worker.run_once() for _ in range(3)]
    finally:
        await contract.close()
    assert [b.claimed for b in batches] == [1, 1, 0]
    costs = conn.execute(
        "SELECT (usage ->> 'cost')::numeric AS cost FROM bee.result WHERE is_current"
    ).fetchall()
    assert [r["cost"] for r in costs] == [Decimal("0.0009")] * 2
    budget = conn.execute("SELECT spent_usd, exhausted FROM bee.budgets").fetchone()
    assert budget == {"spent_usd": Decimal("0.0018"), "exhausted": True}
