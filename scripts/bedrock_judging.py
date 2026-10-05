"""Bedrock Converse transport for independent CLI judges; no inference on import.

The adapter implements the canonical model callback while binding configuration
by judge slot, so identical model IDs can still use different regions/settings.
Converse API: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time


class BedrockCallFailed(RuntimeError):
    """The provider did not return one complete, usable text completion."""


_SLOT_FIELDS = {"model", "region", "profile", "max_tokens", "temperature", "timeout_seconds",
                "additional_model_request_fields", "prices_per_million", "structured_output"}
_PRICE_FIELDS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
# Model-native fields must not replace the prompt, route the request elsewhere,
# add tools, or conceal inference settings managed by this adapter.
_RESERVED_EXTRA = {
    "model", "modelid", "messages", "system", "inferenceconfig", "maxtokens", "temperature",
    "tools", "toolchoice", "toolconfig", "stream", "stop", "stopsequences", "stopsequence",
    "endpoint", "endpointurl", "baseurl", "url", "headers", "extraheaders", "extrabody",
    "extraquery", "timeout", "region", "profile", "authorization", "apikey", "credentials",
    "awsaccesskeyid", "awssecretaccesskey", "awssessiontoken", "accesskey", "secretkey",
    "accesskeyid", "secretaccesskey", "sessiontoken", "additionalmodelrequestfields",
    "outputconfig", "responseformat", "structuredoutput", "structuredoutputs", "strict",
}
_STRUCTURED_OUTPUT_POLICY = "bedrock_assertion_json_schema/1"


def _number(value, field, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (value <= 0 if positive else value < 0)):
        raise ValueError(f"{field} must be a finite {'positive' if positive else 'nonnegative'} number")
    return value


def _extra_fields(value):
    if not isinstance(value, dict):
        raise ValueError("additional_model_request_fields must be an object")

    def check(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ValueError("additional_model_request_fields keys must be strings")
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _RESERVED_EXTRA:
                    raise ValueError("additional_model_request_fields contains a managed, tool, endpoint, or credential field")
                check(item)
        elif isinstance(node, list):
            for item in node:
                check(item)
        elif node is not None and not isinstance(node, (str, bool, int, float)):
            raise ValueError("additional_model_request_fields must contain only JSON values")
        elif isinstance(node, float) and not math.isfinite(node):
            raise ValueError("additional_model_request_fields numbers must be finite")

    check(value)
    return deepcopy(value)


def _slot(raw):
    if not isinstance(raw, dict) or set(raw) - _SLOT_FIELDS:
        raise ValueError("Bedrock role configuration has unknown fields; endpoint/credential overrides are not accepted")
    result = {}
    for field in ("model", "region"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"Bedrock {field} must be a nonempty string without surrounding whitespace")
        result[field] = value
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", result["region"]):
        raise ValueError("Bedrock region must be an AWS region identifier")
    if ":prompt/" in result["model"]:
        raise ValueError("Prompt-management ARNs cannot be used with this adapter's managed system and inference fields")
    profile = raw.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip() or profile != profile.strip()):
        raise ValueError("Bedrock profile must be a nonempty string or null")
    result["profile"] = profile
    tokens = raw.get("max_tokens", 4096)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise ValueError("Bedrock max_tokens must be a positive integer")
    result["max_tokens"] = tokens
    temperature = raw.get("temperature")
    if temperature is not None:
        _number(temperature, "temperature")
        if temperature > 1:
            raise ValueError("Converse temperature must be between 0 and 1")
    result["temperature"] = temperature
    result["timeout_seconds"] = _number(raw.get("timeout_seconds", 120), "timeout_seconds", positive=True)
    structured_output = raw.get("structured_output", False)
    if not isinstance(structured_output, bool):
        raise ValueError("Bedrock structured_output must be a boolean")
    result["structured_output"] = structured_output
    result["additional_model_request_fields"] = _extra_fields(raw.get("additional_model_request_fields", {}))
    prices = raw.get("prices_per_million", {})
    if not isinstance(prices, dict) or set(prices) - _PRICE_FIELDS:
        raise ValueError("prices_per_million must map supported token classes to USD rates")
    result["prices_per_million"] = {key: _number(value, f"prices_per_million.{key}") for key, value in prices.items()}
    return result


def validate_config(raw):
    """Validate every role before any client or output directory is created."""
    if (not isinstance(raw, dict) or set(raw) - {"schema", "judges", "assertion_author"}
            or raw.get("schema") != "bedrock_judges/1"):
        raise ValueError("Expected a bedrock_judges/1 configuration")
    judges = raw.get("judges")
    if not isinstance(judges, list) or len(judges) != 2:
        raise ValueError("Bedrock configuration must contain exactly two judge slots")
    result = {"schema": "bedrock_judges/1", "judges": [_slot(slot) for slot in judges]}
    if "assertion_author" in raw:
        result["assertion_author"] = _slot(raw["assertion_author"])
    return result


def _json_safe(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _write_json(path, value):
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _judgment_output_config(prompt):
    """Constrain the assertion response shape, without claiming semantic validity.

    Only the canonical per-requirement judgment prompt supplies the frozen IDs.
    Completeness, uniqueness, line bounds, nonempty explanations and semantic
    evidence checks remain the caller's local validation responsibilities.
    """
    try:
        packet = json.loads(prompt)
    except (ValueError, TypeError) as exc:
        raise ValueError("structured_output requires a canonical assertion judgment JSON prompt") from exc
    if not isinstance(packet, dict):
        raise ValueError("structured_output requires a canonical assertion judgment JSON object")
    target = packet.get("target_requirement_id")
    assertions = packet.get("assertions")
    if (not isinstance(target, str) or not target.strip()
            or not isinstance(assertions, list) or not assertions
            or not isinstance(packet.get("sysml_with_line_numbers"), str)):
        raise ValueError("structured_output requires a target requirement, frozen assertions and numbered SysML")
    ids = [assertion.get("id") if isinstance(assertion, dict) else None for assertion in assertions]
    if any(not isinstance(aid, str) or not aid.strip() for aid in ids) or len(set(ids)) != len(ids):
        raise ValueError("structured_output requires unique nonempty frozen assertion IDs")
    evidence = {"type": "object", "properties": {
        "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
        "required": ["start_line", "end_line"], "additionalProperties": False}
    verdict = {"type": "object", "properties": {
        "id": {"type": "string", "enum": ids},
        "status": {"type": "string", "enum": ["pass", "fail", "unresolved"]},
        "rationale": {"type": "string"},
        "evidence": {"type": "array", "items": evidence},
        "counterexample": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": ["id", "status", "rationale", "evidence", "counterexample"],
        "additionalProperties": False}
    schema = {"type": "object", "properties": {
        "requirement_id": {"type": "string", "enum": [target]},
        "assertions": {"type": "array", "items": verdict}},
        "required": ["requirement_id", "assertions"], "additionalProperties": False}
    return {"textFormat": {"type": "json_schema", "structure": {"jsonSchema": {
        "name": "canonical_assertion_judgment", "schema": json.dumps(schema, ensure_ascii=False)}}}}


def _normalize(response):
    if not isinstance(response, dict):
        raise BedrockCallFailed("Converse returned a non-object response")
    message = response.get("output", {}).get("message", {})
    blocks = message.get("content", [])
    if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
        raise BedrockCallFailed("Converse returned malformed message content")
    stop = response.get("stopReason")
    texts = [block["text"] for block in blocks if isinstance(block.get("text"), str)]
    text = "\n".join(texts)
    if any("toolUse" in block or "toolResult" in block for block in blocks) or stop == "tool_use":
        status = "unexpected_tool_use"
    elif stop in {"max_tokens", "model_context_window_exceeded"}:
        status = "truncated"
    elif stop != "end_turn":
        # This adapter never supplies stop sequences, so an early stop is not
        # accepted as a complete assessment merely because its prefix is JSON.
        status = "incomplete_completion"
    elif message.get("role") != "assistant" or not text.strip():
        status = "invalid_completion"
    else:
        status = "ok"
    raw_usage = response.get("usage", {})
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    usage = {"input_tokens": _token_count(raw_usage.get("inputTokens")),
             "output_tokens": _token_count(raw_usage.get("outputTokens")),
             "total_tokens": _token_count(raw_usage.get("totalTokens")),
             "cache_read_tokens": _token_count(raw_usage.get("cacheReadInputTokens", 0)),
             "cache_write_tokens": _token_count(raw_usage.get("cacheWriteInputTokens", 0))}
    metrics = response.get("metrics", {})
    return {"status": status, "text": text,
            "reasoning": [block["reasoningContent"] for block in blocks if "reasoningContent" in block],
            "usage": usage, "raw_usage": raw_usage, "stop_reason": stop,
            "provider_latency_ms": metrics.get("latencyMs") if isinstance(metrics, dict) else None}


def _cost(usage, prices):
    if any(usage.get(key) is None for key in _PRICE_FIELDS):
        return {"usd": None, "reason": "Missing or invalid usage", "components_usd": {}}
    if usage["cache_read_tokens"] or usage["cache_write_tokens"]:
        return {"usd": None, "reason": "Cache usage reported; cache billing accounting is not configured", "components_usd": {}}
    components = {key: 0.0 if usage[key] == 0 else usage[key] * prices[key] / 1_000_000 if key in prices else None
                  for key in ("input_tokens", "output_tokens")}
    if any(value is None for value in components.values()):
        return {"usd": None, "reason": "Missing user-supplied token price", "components_usd": components}
    return {"usd": sum(components.values()), "reason": "Estimate from user-supplied USD rates; not an AWS invoice", "components_usd": components}


def _default_client(config):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("Bedrock judging requires the optional requirements-bedrock.txt dependencies") from exc
    session = boto3.Session(profile_name=config["profile"], region_name=config["region"])
    return session.client("bedrock-runtime", config=Config(
        region_name=config["region"], read_timeout=config["timeout_seconds"],
        connect_timeout=min(20, config["timeout_seconds"]),
        retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True))


class BedrockTransport:
    """Bind each role to one client and record every call without hidden retries.

    ``client_factory`` is an optional offline-test hook accepting a normalized
    role configuration and returning an object with ``converse(**request)``.
    Judge indices are zero-based. Log directories are deliberately separate
    from canonical assessment files and cannot be reused/overwritten.
    """

    def __init__(self, config, client_factory=None):
        self.config = validate_config(config)
        self.judge_configs = deepcopy(self.config["judges"])
        self._client_factory = client_factory or _default_client
        self._clients = {}

    def for_judge(self, index):
        if isinstance(index, bool) or not isinstance(index, int) or index not in (0, 1):
            raise ValueError("Judge index must be 0 or 1")
        return self._callback(f"judge-{index + 1}", self.config["judges"][index])

    def for_assertion_author(self):
        if "assertion_author" not in self.config:
            raise ValueError("Bedrock assertion drafting needs an explicit assertion_author configuration")
        return self._callback("assertion-author", self.config["assertion_author"])

    def _callback(self, role, config):
        def call(system, prompt, model, directory, call_id):
            if model != config["model"]:
                raise ValueError("Requested model does not match the bound Bedrock role")
            if any(not isinstance(value, str) or not value.strip() for value in (system, prompt)):
                raise ValueError("Bedrock system and prompt must be nonempty text")
            if not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", call_id):
                raise ValueError("Bedrock call_id must be a safe nonempty identifier")
            call_dir = Path(directory) / "bedrock_calls" / role / call_id
            call_dir.mkdir(parents=True, exist_ok=False)
            inference = {"maxTokens": config["max_tokens"]}
            if config["temperature"] is not None:
                inference["temperature"] = config["temperature"]
            request = {"modelId": config["model"], "system": [{"text": system}],
                       "messages": [{"role": "user", "content": [{"text": prompt}]}], "inferenceConfig": inference}
            if config["additional_model_request_fields"]:
                request["additionalModelRequestFields"] = deepcopy(config["additional_model_request_fields"])
            structured = {"policy": _STRUCTURED_OUTPUT_POLICY,
                          "enabled": config["structured_output"], "schema_in_request": False,
                          "scope": "canonical_assertion_judgment_only",
                          "status": "disabled" if not config["structured_output"] else "not_applicable",
                          "local_validation": "required_by_canonical_assertion_validator",
                          "semantic_correctness_guaranteed": False}
            applicable = config["structured_output"] and role.startswith("judge-") and call_id == "judgment"
            if applicable:
                structured["status"] = "pending_prompt_validation"
            logged_config = {"role": role, "api": "converse", "configuration": config,
                             "sdk_total_max_attempts": 1, "structured_output": structured}
            _write_json(call_dir / "config.json", logged_config)
            _write_json(call_dir / "request.json", request)
            started, tick = _utc(), time.monotonic()
            result = None
            try:
                if applicable:
                    try:
                        request["outputConfig"] = _judgment_output_config(prompt)
                    except ValueError:
                        structured["status"] = "invalid_judgment_prompt"
                        _write_json(call_dir / "config.json", logged_config)
                        raise
                    structured.update(status="requested", schema_in_request=True)
                    _write_json(call_dir / "config.json", logged_config)
                    _write_json(call_dir / "request.json", request)
                if role not in self._clients:
                    self._clients[role] = self._client_factory(deepcopy(config))
                response = self._clients[role].converse(**request)
                _write_json(call_dir / "response.json", response)
                result = _normalize(response)
                result.update(started=started, ended=_utc(), seconds=time.monotonic() - tick,
                              cost=_cost(result["usage"], config["prices_per_million"]),
                              structured_output=deepcopy(structured))
                _write_json(call_dir / "result.json", result)
                (call_dir / "text.txt").write_text(result["text"], encoding="utf-8")
                if result["status"] != "ok":
                    raise BedrockCallFailed(f"Bedrock completion status: {result['status']}")
                return result["text"]
            except BaseException as exc:
                if result is None:
                    raw_error = getattr(exc, "response", {})
                    provider_error = raw_error.get("Error") if isinstance(raw_error, dict) else None
                    _write_json(call_dir / "result.json", {
                        "status": ("interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit))
                                   else "invalid_request" if structured["status"] == "invalid_judgment_prompt"
                                   else "api_error"),
                        "started": started, "ended": _utc(), "seconds": time.monotonic() - tick,
                        "error_type": type(exc).__name__, "error": str(exc), "provider_error": provider_error,
                        "usage": {}, "cost": {"usd": None, "reason": "Completion usage unavailable"},
                        "structured_output": deepcopy(structured)})
                raise
        return call
