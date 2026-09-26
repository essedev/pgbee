"""JSON schema for structured model output, derived from the declared output type."""

from __future__ import annotations

from typing import Any

import jsonschema

CONFIDENCE_SCHEMA: dict[str, Any] = {
    "type": "number",
    "minimum": 0,
    "maximum": 1,
    "description": "How sure you are of the value, from 0 (a guess) to 1 (certain).",
}


def value_schema(output_type: str, output_schema: Any) -> dict[str, Any]:
    """Schema of the `value` field alone."""
    constraints: dict[str, Any] = output_schema if isinstance(output_schema, dict) else {}
    match output_type:
        case "enum":
            return {"type": "string", "enum": list(output_schema)}
        case "text":
            schema: dict[str, Any] = {"type": "string"}
            if "max_length" in constraints:
                schema["maxLength"] = int(constraints["max_length"])
            return schema
        case "boolean":
            return {"type": "boolean"}
        case "integer":
            schema = {"type": "integer"}
            for key, target in (("min", "minimum"), ("max", "maximum")):
                if key in constraints:
                    schema[target] = constraints[key]
            return schema
        case "numeric":
            schema = {"type": "number"}
            for key, target in (("min", "minimum"), ("max", "maximum")):
                if key in constraints:
                    schema[target] = constraints[key]
            return schema
        case "jsonb":
            if not constraints:
                return {"type": "object"}
            return dict(constraints)
        case _:
            raise ValueError(f"output_type {output_type} has no JSON schema (not an llm type)")


def response_schema(output_type: str, output_schema: Any) -> dict[str, Any]:
    """Schema of the whole model response: {value, confidence}."""
    return {
        "type": "object",
        "properties": {
            "value": value_schema(output_type, output_schema),
            "confidence": CONFIDENCE_SCHEMA,
        },
        "required": ["value", "confidence"],
        "additionalProperties": False,
    }


def validate_response(
    output_type: str, output_schema: Any, payload: Any
) -> tuple[Any, float | None]:
    """Check a parsed model response against the schema and return (value, confidence)."""
    jsonschema.validate(payload, response_schema(output_type, output_schema))
    confidence = payload.get("confidence")
    return payload["value"], (float(confidence) if confidence is not None else None)
