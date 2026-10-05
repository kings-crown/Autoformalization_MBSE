"""Offline Bedrock transport tests: no AWS credentials or network needed."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bedrock_judging import BedrockCallFailed, BedrockTransport, _default_client, validate_config


def configuration():
    return {"schema": "bedrock_judges/1", "judges": [
        {"model": "example.model", "region": "us-east-1"},
        {"model": "example.model", "region": "us-west-2", "max_tokens": 2048, "temperature": 0.2}]}


def response(text='{"assertions": []}', stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": stop, "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
            "metrics": {"latencyMs": 321}, "ResponseMetadata": {"RequestId": "test-request", "HTTPStatusCode": 200}}


def result_at(directory, role="judge-1", call_id="judgment"):
    return json.loads((Path(directory) / "bedrock_calls" / role / call_id / "result.json").read_text())


def judgment_prompt():
    return json.dumps({"target_requirement_id": "REQ-1", "assertions": [
        {"id": "REQ-1_boundary"}, {"id": "REQ-1_coverage"}],
        "sysml_with_line_numbers": "1: require constraint { speed <= 10 }\n"})


class BedrockConfigTests(unittest.TestCase):
    def test_defaults_and_same_model_distinct_roles_are_supported_without_importing_sdk(self):
        raw = configuration()
        original = deepcopy(raw)
        with patch.dict(sys.modules, {"boto3": None}):
            normalized = validate_config(raw)
            BedrockTransport(raw)
        self.assertEqual(raw, original)
        self.assertEqual(normalized["judges"][0]["max_tokens"], 4096)
        self.assertEqual(normalized["judges"][0]["timeout_seconds"], 120)
        self.assertIsNone(normalized["judges"][0]["temperature"])
        self.assertIs(normalized["judges"][0]["structured_output"], False)
        self.assertEqual(validate_config(normalized), normalized)
        json.dumps(normalized, allow_nan=False)

    def test_invalid_roles_fail_before_creating_clients(self):
        cases = [{}, {"schema": "unknown", "judges": []}, {"schema": "bedrock_judges/1", "judges": []}]
        for field, value in (("region", "https://elsewhere.example"), ("model", ""), ("profile", " "),
                             ("max_tokens", True), ("max_tokens", 0), ("max_tokens", 1.5),
                             ("temperature", float("nan")), ("temperature", 1.1), ("temperature", False),
                             ("timeout_seconds", float("inf")), ("timeout_seconds", 0),
                             ("structured_output", None), ("structured_output", 1),
                             ("structured_output", 0), ("structured_output", "true"),
                             ("structured_output", {}), ("structured_outputs", True),
                             ("endpoint_url", "https://elsewhere.example"), ("aws_secret_access_key", "do-not-log"),
                             ("prices_per_million", {"input_tokens": -1}),
                             ("prices_per_million", {"input_tokens": float("nan")}),
                             ("prices_per_million", {"made_up_class": 5}),
                             ("model", "arn:aws:bedrock:us-east-1:123456789012:prompt/ABC:1")):
            value_config = configuration()
            value_config["judges"][0][field] = value
            cases.append(value_config)
        factory = Mock()
        for raw in cases:
            with self.subTest(config=raw), self.assertRaises(ValueError):
                BedrockTransport(raw, client_factory=factory)
        factory.assert_not_called()

    def test_additional_fields_cannot_override_prompt_tools_endpoint_or_credentials(self):
        for field in ("messages", "max_tokens", "temperature", "toolConfig", "base_url", "endpointUrl",
                      "AWS_SECRET_ACCESS_KEY", "authorization", "extra_headers", "stop_sequences",
                      "outputConfig", "output_config", "response_format", "structured_output", "strict"):
            raw = configuration()
            raw["judges"][0]["additional_model_request_fields"] = {"nested": {field: "invalid"}}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_config(raw)
        for fields in (["bad"], {"thinking": {"budget_tokens": float("inf")}}, {1: "bad"}, {"a": object()}):
            raw = configuration()
            raw["judges"][0]["additional_model_request_fields"] = fields
            with self.subTest(fields=str(fields)), self.assertRaises(ValueError):
                validate_config(raw)
        raw = configuration()
        raw["judges"][0]["additional_model_request_fields"] = {"thinking": {"type": "enabled", "budget_tokens": 1024}}
        self.assertEqual(validate_config(raw)["judges"][0]["additional_model_request_fields"],
                         raw["judges"][0]["additional_model_request_fields"])

    def test_assertion_author_requires_own_explicit_valid_configuration(self):
        transport = BedrockTransport(configuration(), Mock())
        with self.assertRaisesRegex(ValueError, "assertion_author"):
            transport.for_assertion_author()
        for index in (-1, 2, True, "1"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                transport.for_judge(index)
        raw = configuration()
        raw["assertion_author"] = {"model": "author", "region": "us-east-1"}
        client = Mock(converse=Mock(return_value=response("source-only assertions")))
        with tempfile.TemporaryDirectory() as tmp:
            result = BedrockTransport(raw, lambda _: client).for_assertion_author()("rubric", "sources", "author", tmp, "assertions")
            self.assertEqual(result, "source-only assertions")
            self.assertEqual(result_at(tmp, "assertion-author", "assertions")["status"], "ok")


class BedrockTransportTests(unittest.TestCase):
    def test_exact_prompt_settings_response_reasoning_usage_and_cost_are_recorded(self):
        raw = configuration()
        raw["judges"][0].update(prices_per_million={"input_tokens": 2.0, "output_tokens": 10.0},
                                additional_model_request_fields={"thinking": {"type": "enabled", "budget_tokens": 1024}})
        data = response()
        data["output"]["message"]["content"].insert(0, {"reasoningContent": {
            "reasoningText": {"text": "Provider-returned reasoning", "signature": "provider-signature"}}})
        data["output"]["message"]["content"].insert(1, {"reasoningContent": {"redactedContent": b"redacted"}})
        client = Mock(converse=Mock(return_value=data))
        factory = Mock(return_value=client)
        transport = BedrockTransport(raw, factory)
        with tempfile.TemporaryDirectory() as tmp:
            actual = transport.for_judge(0)("Independent judge rubric", "Requirement and SysML", "example.model", tmp, "judgment")
            log = Path(tmp) / "bedrock_calls" / "judge-1" / "judgment"
            request = json.loads((log / "request.json").read_text())
            result = result_at(tmp)
            logged_response = json.loads((log / "response.json").read_text())
            logged_config = json.loads((log / "config.json").read_text())
            self.assertEqual((log / "text.txt").read_text(), actual)
        self.assertEqual(actual, '{"assertions": []}')
        self.assertEqual(client.converse.call_args.kwargs, request)
        self.assertEqual(request["system"], [{"text": "Independent judge rubric"}])
        self.assertEqual(request["messages"], [{"role": "user", "content": [{"text": "Requirement and SysML"}]}])
        self.assertEqual(request["inferenceConfig"], {"maxTokens": 4096})
        self.assertNotIn("outputConfig", request)
        self.assertEqual(result["structured_output"]["status"], "disabled")
        self.assertFalse(result["structured_output"]["schema_in_request"])
        self.assertEqual(request["additionalModelRequestFields"], raw["judges"][0]["additional_model_request_fields"])
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["raw_usage"], data["usage"])
        self.assertEqual(result["provider_latency_ms"], 321)
        self.assertGreaterEqual(result["seconds"], 0)
        self.assertAlmostEqual(result["cost"]["usd"], 0.0007)
        self.assertEqual(result["reasoning"][0]["reasoningText"]["text"], "Provider-returned reasoning")
        self.assertEqual(logged_response["output"]["message"]["content"][1]["reasoningContent"]["redactedContent"]["encoding"], "base64")
        self.assertEqual(logged_config["sdk_total_max_attempts"], 1)
        factory.assert_called_once()

    def test_structured_judgment_request_constrains_contract_and_records_scope(self):
        raw = configuration()
        raw["judges"][0]["structured_output"] = True
        client = Mock(converse=Mock(return_value=response()))
        with tempfile.TemporaryDirectory() as tmp:
            BedrockTransport(raw, lambda _: client).for_judge(0)(
                "rubric", judgment_prompt(), "example.model", tmp, "judgment")
            log = Path(tmp) / "bedrock_calls/judge-1/judgment"
            request = json.loads((log / "request.json").read_text())
            config = json.loads((log / "config.json").read_text())
            result = result_at(tmp)
        self.assertEqual(request, client.converse.call_args.kwargs)
        self.assertEqual(request["messages"][0]["content"], [{"text": judgment_prompt()}])
        output = request["outputConfig"]["textFormat"]
        self.assertEqual(output["type"], "json_schema")
        self.assertEqual(output["structure"]["jsonSchema"]["name"], "canonical_assertion_judgment")
        schema = json.loads(output["structure"]["jsonSchema"]["schema"])
        self.assertEqual(set(schema["required"]), {"requirement_id", "assertions"})
        self.assertEqual(schema["properties"]["requirement_id"], {"type": "string", "enum": ["REQ-1"]})
        verdict = schema["properties"]["assertions"]["items"]
        self.assertEqual(verdict["properties"]["id"]["enum"], ["REQ-1_boundary", "REQ-1_coverage"])
        self.assertEqual(verdict["properties"]["status"]["enum"], ["pass", "fail", "unresolved"])
        self.assertEqual(set(verdict["required"]), {"id", "status", "rationale", "evidence", "counterexample"})
        self.assertEqual(verdict["properties"]["rationale"], {"type": "string"})
        self.assertEqual(verdict["properties"]["counterexample"], {"anyOf": [{"type": "string"}, {"type": "null"}]})
        span = verdict["properties"]["evidence"]["items"]
        self.assertEqual(span["properties"], {"start_line": {"type": "integer"}, "end_line": {"type": "integer"}})
        self.assertEqual(set(span["required"]), {"start_line", "end_line"})
        for node in (schema, verdict, span):
            self.assertIs(node["additionalProperties"], False)
        for unsupported in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems"):
            self.assertNotIn('"' + unsupported + '"', json.dumps(schema))
        receipt = result["structured_output"]
        self.assertEqual(receipt, config["structured_output"])
        self.assertEqual(receipt["policy"], "bedrock_assertion_json_schema/1")
        self.assertEqual(receipt["scope"], "canonical_assertion_judgment_only")
        self.assertEqual(receipt["status"], "requested")
        self.assertTrue(receipt["schema_in_request"])
        self.assertFalse(receipt["semantic_correctness_guaranteed"])
        self.assertEqual(receipt["local_validation"], "required_by_canonical_assertion_validator")

    def test_structured_output_does_not_apply_to_assertion_authorship_or_other_call_purposes(self):
        raw = configuration()
        raw["judges"][0]["structured_output"] = True
        raw["assertion_author"] = {"model": "author", "region": "us-east-1", "structured_output": True}
        client = Mock(converse=Mock(return_value=response("plain response")))
        transport = BedrockTransport(raw, lambda _: client)
        for role, callback, model, call_id in (
                ("judge-1", transport.for_judge(0), "example.model", "other"),
                ("assertion-author", transport.for_assertion_author(), "author", "assertions"),
                ("assertion-author", transport.for_assertion_author(), "author", "judgment")):
            with self.subTest(role=role, call_id=call_id), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(callback("rubric", "non-JSON prompt", model, tmp, call_id), "plain response")
                self.assertNotIn("outputConfig", client.converse.call_args.kwargs)
                receipt = result_at(tmp, role, call_id)["structured_output"]
                self.assertTrue(receipt["enabled"])
                self.assertFalse(receipt["schema_in_request"])
                self.assertEqual(receipt["status"], "not_applicable")

    def test_invalid_structured_judgment_prompts_fail_before_client_creation_with_receipts(self):
        raw = configuration()
        raw["judges"][0]["structured_output"] = True
        base = json.loads(judgment_prompt())
        cases = ["not JSON", "[]", "{}"]
        for field, value in (("target_requirement_id", ""), ("target_requirement_id", 7),
                             ("assertions", []), ("assertions", [{"id": "same"}, {"id": "same"}]),
                             ("assertions", [{"id": ["not a string"]}]), ("assertions", [None]),
                             ("sysml_with_line_numbers", None)):
            packet = deepcopy(base)
            packet[field] = value
            cases.append(json.dumps(packet))
        factory = Mock()
        transport = BedrockTransport(raw, factory)
        for prompt in cases:
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "structured_output"):
                    transport.for_judge(0)("rubric", prompt, "example.model", tmp, "judgment")
                result = result_at(tmp)
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["structured_output"]["status"], "invalid_judgment_prompt")
                self.assertFalse(result["structured_output"]["schema_in_request"])
                self.assertIsNone(result["cost"]["usd"])
                log = Path(tmp) / "bedrock_calls/judge-1/judgment"
                self.assertTrue((log / "request.json").is_file())
                self.assertEqual(json.loads((log / "config.json").read_text())["structured_output"],
                                 result["structured_output"])
        factory.assert_not_called()

    def test_provider_rejection_of_structured_output_is_logged_without_fallback(self):
        class UnsupportedStructuredOutput(Exception):
            response = {"Error": {"Code": "ValidationException", "Message": "Structured output unsupported"}}
        raw = configuration()
        raw["judges"][0]["structured_output"] = True
        client = Mock(converse=Mock(side_effect=UnsupportedStructuredOutput("unsupported schema or model")))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(UnsupportedStructuredOutput):
                BedrockTransport(raw, lambda _: client).for_judge(0)(
                    "rubric", judgment_prompt(), "example.model", tmp, "judgment")
            result = result_at(tmp)
            self.assertEqual(result["status"], "api_error")
            self.assertEqual(result["provider_error"]["Code"], "ValidationException")
            self.assertEqual(result["structured_output"]["status"], "requested")
            self.assertIsNone(result["cost"]["usd"])
            request = json.loads((Path(tmp) / "bedrock_calls/judge-1/judgment/request.json").read_text())
            self.assertIn("outputConfig", request)
        client.converse.assert_called_once()

    def test_failed_structured_completion_keeps_response_usage_and_cost(self):
        raw = configuration()
        raw["judges"][0].update(structured_output=True,
                                prices_per_million={"input_tokens": 2, "output_tokens": 10})
        data = response('{"requirement_id": "REQ-1",', stop="max_tokens")
        client = Mock(converse=Mock(return_value=data))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BedrockCallFailed):
                BedrockTransport(raw, lambda _: client).for_judge(0)(
                    "rubric", judgment_prompt(), "example.model", tmp, "judgment")
            result = result_at(tmp)
            self.assertEqual(result["status"], "truncated")
            self.assertEqual(result["usage"]["output_tokens"], 50)
            self.assertAlmostEqual(result["cost"]["usd"], 0.0007)
            self.assertTrue(result["structured_output"]["schema_in_request"])
            log = Path(tmp) / "bedrock_calls/judge-1/judgment"
            self.assertEqual(json.loads((log / "response.json").read_text()), data)
            self.assertEqual((log / "text.txt").read_text(), result["text"])
        client.converse.assert_called_once()

    def test_identical_models_use_separate_clients_and_settings_without_shared_conversations(self):
        clients = [Mock(converse=Mock(return_value=response("first"))), Mock(converse=Mock(return_value=response("second")))]
        factory = Mock(side_effect=clients)
        transport = BedrockTransport(configuration(), factory)
        with tempfile.TemporaryDirectory() as tmp:
            first = transport.for_judge(0)("rubric", "same prompt", "example.model", tmp, "judgment")
            second = transport.for_judge(1)("rubric", "same prompt", "example.model", tmp, "judgment")
            transport.for_judge(0)("rubric", "new prompt", "example.model", tmp, "another")
            self.assertEqual(result_at(tmp, "judge-1")["text"], "first")
            self.assertEqual(result_at(tmp, "judge-2")["text"], "second")
        self.assertEqual((first, second), ("first", "second"))
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args[0]["region"] for call in factory.call_args_list], ["us-east-1", "us-west-2"])
        self.assertEqual(clients[0].converse.call_count, 2)
        self.assertEqual(clients[1].converse.call_args.kwargs["inferenceConfig"], {"maxTokens": 2048, "temperature": 0.2})
        self.assertEqual(len(clients[0].converse.call_args.kwargs["messages"]), 1)

    def test_truncated_tool_guardrail_empty_and_early_stop_responses_fail_with_evidence_and_no_retry(self):
        cases = [(response(stop="max_tokens"), "truncated"),
                 (response(stop="model_context_window_exceeded"), "truncated"),
                 (response(stop="guardrail_intervened"), "incomplete_completion"),
                 (response(stop="stop_sequence"), "incomplete_completion"),
                 (response(""), "invalid_completion"),
                 (response(stop="tool_use"), "unexpected_tool_use")]
        tool = response()
        tool["output"]["message"]["content"].append({"toolUse": {"toolUseId": "1", "name": "tool", "input": {}}})
        cases.append((tool, "unexpected_tool_use"))
        for data, status in cases:
            client = Mock(converse=Mock(return_value=data))
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(BedrockCallFailed):
                    BedrockTransport(configuration(), lambda _: client).for_judge(0)("rubric", "prompt", "example.model", tmp, "judgment")
                self.assertEqual(result_at(tmp)["status"], status)
                self.assertEqual(result_at(tmp)["usage"]["output_tokens"], 50)
                self.assertTrue((Path(tmp) / "bedrock_calls/judge-1/judgment/response.json").is_file())
                client.converse.assert_called_once()

    def test_api_failure_preserves_request_and_unknown_cost_without_retry(self):
        class FakeAPIError(Exception):
            response = {"Error": {"Code": "ThrottlingException", "Message": "Try later"},
                        "ResponseMetadata": {"HTTPHeaders": {"irrelevant": "not-copied"}}}
        client = Mock(converse=Mock(side_effect=FakeAPIError("rate limited")))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FakeAPIError):
                BedrockTransport(configuration(), lambda _: client).for_judge(0)("rubric", "prompt", "example.model", tmp, "judgment")
            result = result_at(tmp)
            self.assertEqual(result["status"], "api_error")
            self.assertEqual(result["provider_error"]["Code"], "ThrottlingException")
            self.assertNotIn("not-copied", json.dumps(result))
            self.assertIsNone(result["cost"]["usd"])
            self.assertEqual(result["usage"], {})
            self.assertTrue((Path(tmp) / "bedrock_calls/judge-1/judgment/request.json").is_file())
        client.converse.assert_called_once()

    def test_usage_and_cache_uncertainty_are_not_zero_cost(self):
        for usage in ({}, {"inputTokens": True, "outputTokens": 1},
                      {"inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 20}):
            data = response()
            data["usage"] = usage
            client = Mock(converse=Mock(return_value=data))
            raw = configuration()
            raw["judges"][0]["prices_per_million"] = {"input_tokens": 2, "output_tokens": 10}
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as tmp:
                BedrockTransport(raw, lambda _: client).for_judge(0)("rubric", "prompt", "example.model", tmp, "judgment")
                self.assertIsNone(result_at(tmp)["cost"]["usd"])
        client = Mock(converse=Mock(return_value=response()))
        with tempfile.TemporaryDirectory() as tmp:
            BedrockTransport(configuration(), lambda _: client).for_judge(0)("rubric", "prompt", "example.model", tmp, "judgment")
            self.assertIsNone(result_at(tmp)["cost"]["usd"])

    def test_model_mismatch_unsafe_ids_and_duplicate_calls_do_not_invoke_provider(self):
        client = Mock(converse=Mock(return_value=response()))
        factory = Mock(return_value=client)
        callback = BedrockTransport(configuration(), factory).for_judge(0)
        with tempfile.TemporaryDirectory() as tmp:
            for model, identifier in (("wrong.model", "judgment"), ("example.model", "../escape")):
                with self.subTest(model=model, identifier=identifier), self.assertRaises(ValueError):
                    callback("rubric", "prompt", model, tmp, identifier)
            factory.assert_not_called()
            callback("rubric", "prompt", "example.model", tmp, "judgment")
            with self.assertRaises(FileExistsError):
                callback("rubric", "different prompt", "example.model", tmp, "judgment")
        client.converse.assert_called_once()

    def test_default_client_uses_sdk_credentials_and_disables_retries_and_endpoint_overrides(self):
        boto3 = types.ModuleType("boto3")
        session = Mock()
        boto3.Session = Mock(return_value=session)
        botocore = types.ModuleType("botocore")
        config_module = types.ModuleType("botocore.config")
        config_module.Config = Mock(return_value="sdk-config")
        config = validate_config(configuration())["judges"][0]
        with patch.dict(sys.modules, {"boto3": boto3, "botocore": botocore, "botocore.config": config_module}):
            _default_client(config)
        boto3.Session.assert_called_once_with(profile_name=None, region_name="us-east-1")
        self.assertEqual(config_module.Config.call_args.kwargs["retries"], {"total_max_attempts": 1})
        self.assertIs(config_module.Config.call_args.kwargs["ignore_configured_endpoint_urls"], True)
        session.client.assert_called_once_with("bedrock-runtime", config="sdk-config")

    def test_missing_optional_sdk_is_an_explicit_recorded_failure(self):
        with patch.dict(sys.modules, {"boto3": None}), tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "requirements-bedrock.txt"):
                BedrockTransport(configuration()).for_judge(0)("rubric", "prompt", "example.model", tmp, "judgment")
            self.assertEqual(result_at(tmp)["status"], "api_error")


if __name__ == "__main__":
    unittest.main()
