"""Model providers. OpenRouter is the reference; the Protocol is what the worker depends on."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2 as httpx
import openai

from aicol.jobs import Job
from aicol.schema import enum_values, response_schema, validate_response

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
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


class Provider(Protocol):
    async def derive(self, job: Job) -> LlmResult: ...

    async def decide(self, jobs: list[Job]) -> list[LlmResult | ProviderError]:
        """One call for decision jobs that share model and source: one question per job.

        Raises when the whole call fails; a ProviderError in the list fails only that job.
        """
        ...

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
    """Map SDK, HTTP and parsing exceptions to a ProviderError with a retry decision."""
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
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = exc.response.text[:300]
        if status == 429:
            return ProviderError(f"rate limited: {body}", retryable=True, rate_limited=True)
        if status >= 500:
            return ProviderError(f"provider {status}: {body}", retryable=True)
        return ProviderError(f"provider {status}: {body}", retryable=False)
    if isinstance(exc, httpx.TransportError):
        return ProviderError(f"connection: {exc}", retryable=True)
    if isinstance(exc, json.JSONDecodeError):
        return ProviderError(f"model returned invalid JSON: {exc}", retryable=True)
    return ProviderError(f"{type(exc).__name__}: {exc}", retryable=True)


def user_message(job: Job) -> str:
    """Prompt plus the row data, plus option descriptions when the enum schema has them."""
    parts = [job.prompt or ""]
    if job.output_type == "enum" and isinstance(job.output_schema, dict):
        parts.append("Options:\n" + "\n".join(f"- {k}: {v}" for k, v in job.output_schema.items()))
    parts.append(f"Data:\n{job.source_text()}")
    return "\n\n".join(parts)


def decision_question(job: Job) -> dict[str, Any]:
    """Translate a derived column into one typed question for a decision model."""
    schema = job.output_schema if isinstance(job.output_schema, dict) else {}
    match job.output_type:
        case "enum":
            return {"type": "choice", "instructions": job.prompt, "criteria": dict(schema)}
        case "boolean":
            criteria = {
                "true": schema.get("true", "The statement holds for this row"),
                "false": schema.get("false", "The statement does not hold"),
            }
            return {"type": "noul", "instructions": job.prompt, "criteria": criteria}
        case "integer" | "numeric":
            return {"type": "score", "instructions": job.prompt, "criteria": list(schema["levels"])}
        case _:
            raise ProviderError(
                f"backend decision cannot produce output_type {job.output_type}", retryable=False
            )


def decision_answer(job: Job, answer: dict[str, Any]) -> tuple[Any, float | None, dict[str, Any]]:
    """Turn a decision answer into (value, confidence, details) for the declared output type."""
    match job.output_type:
        case "enum":
            value = answer["choice"]
            if value not in enum_values(job.output_schema):
                raise ProviderError(f"decision model chose unknown value {value!r}", retryable=True)
            return (
                value,
                float(answer["confidence"]),
                {"probabilities": answer.get("probabilities")},
            )
        case "boolean":
            p = float(answer["noul"])
            return p >= 0.5, max(p, 1.0 - p), {"probability_true": p}
        case "integer":
            score = float(answer["score"])
            details = {
                "score": score,
                "probabilities": answer.get("probabilities"),
                "legend": answer.get("legend"),
            }
            return int(round(score)), float(answer["confidence"]), details
        case "numeric":
            score = float(answer["score"])
            details = {"probabilities": answer.get("probabilities"), "legend": answer.get("legend")}
            return score, float(answer["confidence"]), details
        case _:
            raise ProviderError(f"unexpected output_type {job.output_type}", retryable=False)


class OpenRouterProvider:
    """OpenRouter via the OpenAI SDK for chat and embeddings, plain HTTP for decisions."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0) -> None:
        headers = {"HTTP-Referer": "https://github.com/essedev/ai-db", "X-Title": "ai-db"}
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            default_headers=headers,
        )
        self._decisions_url = base_url.rstrip("/").removesuffix("/v1") + "/alpha/decisions"
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", **headers},
        )

    async def derive(self, job: Job) -> LlmResult:
        assert job.prompt is not None
        started = time.monotonic()
        params: dict[str, Any] = {}
        if "temperature" in job.backend_config:
            params["temperature"] = float(job.backend_config["temperature"])
        if "max_tokens" in job.backend_config:
            params["max_completion_tokens"] = int(job.backend_config["max_tokens"])
        extra_body: dict[str, Any] = {"usage": {"include": True}}
        if "reasoning" in job.backend_config:
            # OpenRouter unified reasoning control, e.g. {"effort": "low"} or {"enabled": false}.
            extra_body["reasoning"] = job.backend_config["reasoning"]
        response = await self._client.chat.completions.create(
            model=job.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message(job)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "derived_value",
                    "strict": True,
                    "schema": response_schema(job.output_type, job.output_schema),
                },
            },
            extra_body=extra_body,
            **params,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        content = response.choices[0].message.content or ""
        payload = parse_json_object(content)
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

    async def decide(self, jobs: list[Job]) -> list[LlmResult | ProviderError]:
        """Typed questions to a decision model (TypeSafe Jev) via OpenRouter's decisions API.

        All jobs share model and source, so they become questions on one state: the state is
        paid once, which is most of the cost.
        """
        first = jobs[0]
        questions = {_question_key(job): decision_question(job) for job in jobs}
        state: Any = first.source_text() if len(first.source) == 1 else first.source
        started = time.monotonic()
        response = await self._http.post(
            self._decisions_url,
            json={"model": first.model, "state": state, "questions": questions},
        )
        response.raise_for_status()
        latency_ms = int((time.monotonic() - started) * 1000)
        body = response.json()
        answers = body.get("answers") or {}
        reported = body.get("usage") or {}
        n = len(jobs)
        usage = {
            k: v / n
            for k, v in {
                "prompt_tokens": reported.get("input_tokens"),
                "completion_tokens": reported.get("output_tokens"),
                "cost": reported.get("cost"),
            }.items()
            if isinstance(v, int | float)
        }
        model = str(body.get("model") or first.model)
        results: list[LlmResult | ProviderError] = []
        for job in jobs:
            answer = answers.get(_question_key(job))
            if not isinstance(answer, dict):
                results.append(ProviderError("decision model returned no answer", retryable=True))
                continue
            try:
                value, confidence, details = decision_answer(job, answer)
            except ProviderError as exc:
                results.append(exc)
                continue
            except (KeyError, TypeError, ValueError) as exc:
                results.append(ProviderError(f"malformed decision answer: {exc!r}", retryable=True))
                continue
            results.append(
                LlmResult(
                    value=value,
                    confidence=confidence,
                    model=model,
                    usage=usage,
                    latency_ms=latency_ms,
                    details={**details, "questions_in_call": n},
                )
            )
        return results

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


def _question_key(job: Job) -> str:
    return f"job_{job.job_id}"


def parse_json_object(text: str) -> Any:
    """Parse the first JSON value in the text, tolerating trailing junk some models append."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    value, _end = json.JSONDecoder().raw_decode(stripped.lstrip())
    return value


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
