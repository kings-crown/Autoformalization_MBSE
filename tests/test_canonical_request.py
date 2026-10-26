"""Offline character-budget and pre-dispatch validation regressions."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from canonical_request import (
    HARD_MAX_INPUT_CHARS, RequestSizeError, compose_request,
    request_preflight, validate_request_text,
)
import requirements_pipeline as transport


class RequestPreflightTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_composition_preserves_existing_framing_and_trims_only_ends(self):
        self.assertEqual(compose_request(" \nsystem\n rule  ", "\t source\n line \n"),
                         "System instructions:\nsystem\n rule\n\nUser request:\nsource\n line\n")

    def test_wrapped_request_just_below_exactly_at_and_above_hard_ceiling(self):
        system = "critical system instruction"
        overhead = len(compose_request(system, ""))
        for delta, status in ((-1, "passed"), (0, "passed"), (1, "oversize")):
            with self.subTest(delta=delta):
                prompt = "x" * (HARD_MAX_INPUT_CHARS - overhead + delta)
                details = request_preflight(system, prompt)
                self.assertEqual(details["chars"], HARD_MAX_INPUT_CHARS + delta)
                self.assertEqual(details["status"], status)
                full = compose_request(system, prompt)
                if delta <= 0:
                    self.assertEqual(validate_request_text(full), details)
                else:
                    with self.assertRaises(RequestSizeError) as raised:
                        validate_request_text(full)
                    self.assertEqual(raised.exception.details, details)

    def test_wrapper_and_system_count_against_lower_study_limit(self):
        full = compose_request("system", "source")
        with patch.dict(os.environ, {"CODEX_MAX_INPUT_CHARS": str(len(full) - 1)}):
            details = request_preflight("system", "source")
            self.assertEqual(details["status"], "oversize")
            self.assertEqual(details["chars"], len(full))
            self.assertEqual(details["max_chars"], len(full) - 1)
        self.assertEqual(request_preflight("system", "source", len(full))["status"], "passed")

    def test_unicode_counts_characters_not_bytes_or_tokens(self):
        full = compose_request("指令", "電圧 ≤ 28 V 🔋")
        details = validate_request_text(full, len(full))
        self.assertEqual(details["chars"], len(full))
        self.assertGreater(len(full.encode("utf-8")), details["chars"])
        self.assertEqual(details["unit"], "characters")
        self.assertIn("not a token", details["scope"])
        with self.assertRaises(RequestSizeError):
            validate_request_text(full, len(full) - 1)

    def test_invalid_environment_never_includes_its_value_in_error(self):
        for value in ("", "  ", "0", "-1", "1.5", "true", "∞", "１２", "source-secret",
                      str(HARD_MAX_INPUT_CHARS + 1)):
            with self.subTest(value=value), patch.dict(os.environ, {"CODEX_MAX_INPUT_CHARS": value}):
                with self.assertRaises(ValueError) as raised:
                    validate_request_text("source", 20)
                self.assertIn("CODEX_MAX_INPUT_CHARS", str(raised.exception))
                self.assertNotIn("source-secret", str(raised.exception))

    def test_explicit_limit_can_only_lower_configured_ceiling(self):
        with patch.dict(os.environ, {"CODEX_MAX_INPUT_CHARS": " 100 "}):
            self.assertEqual(validate_request_text("x", 50)["max_chars"], 50)
            for value in (0, -1, True, "50", 50.0, 101, HARD_MAX_INPUT_CHARS + 1):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    validate_request_text("x", value)

    def test_non_text_rejected_without_source_echo(self):
        for value in (None, 1, True, b"source-secret", {"source-secret": True}, ["source-secret"]):
            with self.subTest(value=value):
                for check in (lambda: compose_request(value, "text"),
                              lambda: compose_request("system", value),
                              lambda: request_preflight("system", value),
                              lambda: validate_request_text(value)):
                    with self.assertRaises(TypeError) as raised:
                        check()
                    self.assertNotIn("source-secret", str(raised.exception))

    def test_size_error_contains_counts_but_no_source(self):
        with self.assertRaises(RequestSizeError) as raised:
            validate_request_text("private-source-secret", 5)
        error = raised.exception
        self.assertEqual(error.details["chars"], 21)
        self.assertEqual(error.details["max_chars"], 5)
        self.assertNotIn("private-source-secret", str(error))
        self.assertNotIn("private-source-secret", json.dumps(error.details))

    def test_raw_transport_rejects_before_cli_lookup_tempfiles_or_process(self):
        for prompt in ("x" * (HARD_MAX_INPUT_CHARS + 1), None, {"source-secret": True}):
            expected = RequestSizeError if isinstance(prompt, str) else TypeError
            with self.subTest(kind=type(prompt).__name__), \
                    patch.object(transport.shutil, "which") as lookup, \
                    patch.object(transport.tempfile, "NamedTemporaryFile") as tempfile, \
                    patch.object(transport.subprocess, "Popen") as process:
                with self.assertRaises(expected):
                    transport._run_codex_exec(prompt, "offline-model")
                lookup.assert_not_called()
                tempfile.assert_not_called()
                process.assert_not_called()

    def test_chat_rejects_oversize_and_non_text_even_when_transport_is_mocked(self):
        for system, source, exception in (("system", "source", RequestSizeError),
                                          ({"private": True}, "source", TypeError),
                                          ("system", None, TypeError)):
            with self.subTest(exception=exception), \
                    patch.dict(os.environ, {"CODEX_MAX_INPUT_CHARS": "10"}), \
                    patch.object(transport, "_run_codex_exec") as run:
                with self.assertRaises(exception):
                    asyncio.run(transport._codex_chat_text(system, source, "offline-model"))
                run.assert_not_called()

    def test_chat_passes_exact_composed_text_to_transport(self):
        with patch.object(transport, "_run_codex_exec", return_value="answer") as run:
            self.assertEqual(asyncio.run(transport._codex_chat_text(" system ", " source ", "offline-model")),
                             "answer")
            run.assert_called_once_with(compose_request(" system ", " source "), "offline-model")


if __name__ == "__main__":
    unittest.main()
