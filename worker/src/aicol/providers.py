"""Model providers. OpenRouter is the reference; the Protocol is what the worker depends on."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import openai

from aicol.jobs import Job
from aicol.schema import response_schema, validate_response

SYSTEM_PROMPT = (
    "You derive one value for a database column from the data of one row. "
    "Follow the instruction, look only at the data given, and answer with a JSON object "
    'with two fields: "value" (the derived value, matching the required schema) and '
    '"confidence" (a number from 0 to 1 saying how sure you are). No other text.'
)


@dataclass(frozen=True)
class LlmResult:
    value: Any
    confidence: float | None
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


class Provider(Protocol):
    async def derive(self, job: Job) -> LlmResult: ...

    async def embed(
        self, model: str, texts: list[str], config: dict[str, Any]
    ) -> EmbeddingResult: ...


class ProviderError(Exception):
    """A failure the worker reports to ai.fail_job. `retryable` decides between backoff and dead."""

    def __init__(self, message: str, *, retryable: bool, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited


def classify(exc: Exception) -> ProviderError:
    """Map SDK and parsing exceptions to a ProviderError with a retry decision."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, openai.RateLimitError):
        return ProviderError(f"rate limited: {exc}", retryable=True, rate_limited=True)
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return ProviderError(f"connection: {exc}", retryable=True)
    if isinstance(exc, openai.InternalServerError):
        return ProviderError(f"provider 5xx: {exc}", retryable=True)
    if isinstance(exc, openai.APIStatusError):
        # 4xx other than 429: the request itself is wrong (auth, model, schema). No point retrying.
        return ProviderError(f"provider {exc.status_code}: {exc}", retryable=False)
    if isinstance(exc, json.JSONDecodeError):
        return ProviderError(f"model returned invalid JSON: {exc}", retryable=True)
    return ProviderError(f"{type(exc).__name__}: {exc}", retryable=True)


class OpenRouterProvider:
    """OpenRouter through the OpenAI SDK: chat completions with JSON schema output, embeddings."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0) -> None:
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            default_headers={
                "HTTP-Referer": "https://github.com/essedev/ai-db",
                "X-Title": "ai-db",
            },
        )

    async def derive(self, job: Job) -> LlmResult:
        assert job.prompt is not None
        started = time.monotonic()
        params: dict[str, Any] = {}
        if "temperature" in job.backend_config:
            params["temperature"] = float(job.backend_config["temperature"])
        if "max_tokens" in job.backend_config:
            params["max_completion_tokens"] = int(job.backend_config["max_tokens"])
        response = await self._client.chat.completions.create(
            model=job.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{job.prompt}\n\nData:\n{job.source_text()}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "derived_value",
                    "strict": True,
                    "schema": response_schema(job.output_type, job.output_schema),
                },
            },
            extra_body={"usage": {"include": True}},
            **params,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        content = response.choices[0].message.content or ""
        payload = json.loads(content)
        try:
            value, confidence = validate_response(job.output_type, job.output_schema, payload)
        except Exception as exc:
            raise ProviderError(f"model output failed schema: {exc}", retryable=True) from exc
        return LlmResult(
            value=value,
            confidence=confidence,
            model=response.model or job.model,
            usage=_usage_dict(response.usage),
            latency_ms=latency_ms,
        )

    async def embed(self, model: str, texts: list[str], config: dict[str, Any]) -> EmbeddingResult:
        started = time.monotonic()
        params: dict[str, Any] = {}
        if "dimensions" in config:
            params["dimensions"] = int(config["dimensions"])
        response = await self._client.embeddings.create(
            model=model, input=texts, encoding_format="float", **params
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        ordered = sorted(response.data, key=lambda d: d.index)
        return EmbeddingResult(
            vectors=[list(d.embedding) for d in ordered],
            model=response.model or model,
            usage=_usage_dict(response.usage),
            latency_ms=latency_ms,
        )


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    data = usage.model_dump(exclude_none=True) if hasattr(usage, "model_dump") else dict(usage)
    keep = {
        k: data[k]
        for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
        if k in data
    }
    return keep
