"""Chat-model construction through the actually installed provider constructors, without credentials or network."""

import json
import logging
import os
import traceback
from types import SimpleNamespace
from unittest.mock import patch

from anthropic.types import Message as AnthropicMessage
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from google.genai import types as genai_types
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from openai.types.chat.parsed_chat_completion import ParsedChatCompletion

from nautobot_agent.models import LLMConfiguration
from nautobot_agent.runtime.llm import build_chat_model
from nautobot_agent.runtime.scope import ScopeDecision, bind_scope_decision, classify_request
from nautobot_agent.tests.support.chat import enable_agent_settings

SENTINEL = "CREDENTIAL-SENTINEL-4d1f"
AUTH_ENVIRONMENT = (
    "OPENAI_API_KEY",
    "OPENAI_ADMIN_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_URL",
)


class BuildChatModelTests(SimpleTestCase):
    """Profiles, native authentication, options and safe failures with the real installed integrations."""

    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key in AUTH_ENVIRONMENT:
            os.environ.pop(key, None)
        self.logged = []
        logged = self.logged

        class RecordingHandler(logging.Handler):
            """Capture rendered log records for secret-leak assertions."""

            def emit(self, record):
                logged.append(record.getMessage() + (record.exc_text or ""))

        handler = RecordingHandler()
        logger = logging.getLogger("nautobot_agent.llm")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

    def build(self, model="openai:gpt-test", *, profile="", parameters=None, credentials=None):
        """Build a model for an unsaved configuration under the given deployment credential profiles."""
        enable_agent_settings(self, credentials)
        configuration = LLMConfiguration(
            name="unsaved", model=model, credential_profile=profile, parameters=parameters or {}
        )
        return build_chat_model(configuration)

    def assert_safe_failure(self, caught, *, expected=ImproperlyConfigured):
        """Assert a failure is the safe configuration error with no credential in any rendering or chain."""
        error = caught.exception
        self.assertIsInstance(error, expected)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__context__ is None or error.__suppress_context__)
        self.assertNotIn(SENTINEL, "".join(traceback.format_exception(error)))

    def test_providers_receive_profile_and_parameters(self):
        credentials = {"local": {"api_key": SENTINEL, "base_url": "http://localhost:11434/v1"}}
        model = self.build(
            "openai:ft:gpt-test:org:id",
            profile="local",
            credentials=credentials,
            parameters={
                "api_key": "parameter-key-overridden-by-profile",
                "temperature": 0.25,
                "extra_body": {"options": {"num_ctx": 4096}},
                "profile": {"max_input_tokens": 8192},
            },
        )

        assert isinstance(model, ChatOpenAI)
        self.assertEqual(model.model_name, "ft:gpt-test:org:id")
        self.assertEqual(model.openai_api_key.get_secret_value(), SENTINEL)
        self.assertEqual(model.openai_api_base, "http://localhost:11434/v1")
        self.assertEqual(model.temperature, 0.25)
        self.assertEqual(model.extra_body, {"options": {"num_ctx": 4096}})
        self.assertEqual(model.profile, {"max_input_tokens": 8192})

    def test_unusable_configuration_fails_safely(self):
        cases = {
            "missing native credentials": {},
            "malformed profile catalog entry": {"profile": "bad", "credentials": {"bad": SENTINEL}},
            "malformed profile catalog": {"profile": "corp", "credentials": [SENTINEL]},
            "unknown provider": {"model": "not-a-provider:x"},
            "resolver rejects reserved parameter": {"parameters": {"model": SENTINEL}},
            "constructor rejects an option": {
                "profile": "corp",
                "credentials": {"corp": {"api_key": SENTINEL, "max_retries": f"not-a-number-{SENTINEL}"}},
            },
            "parameter rejected": {
                "model": "anthropic:claude-test",
                "parameters": {"max_tokens": f"not-a-number-{SENTINEL}"},
            },
        }

        for label, options in cases.items():
            with self.subTest(label):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    self.build(**options)

                self.assert_safe_failure(caught)
                self.assertNotIn(SENTINEL, "\n".join(self.logged))


class NativeScopeDecisionParsingTests(SimpleTestCase):
    """Exercise representative native SDK parsers with deterministic boundary responses."""

    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key in (
            *AUTH_ENVIRONMENT,
            "ANTHROPIC_AUTH_TOKEN",
            "GOOGLE_API_KEY",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GCLOUD_PROJECT",
            "OLLAMA_HOST",
            "OPENROUTER_API_KEY",
            "DEEPSEEK_API_KEY",
            "VERTEXAI_PROJECT",
            "VERTEXAI_LOCATION",
        ):
            os.environ.pop(key, None)

    def build(self, model, *, parameters):
        """Build a native classifier model through the application configuration path."""
        enable_agent_settings(self)
        configuration = LLMConfiguration(name="unsaved", model=model, parameters=parameters)
        return build_chat_model(configuration)

    def _sdk_response(self, provider, allowed):
        """Return a provider-shaped SDK response containing the supplied decision value."""
        structured = {"allowed": allowed}
        if provider in {"openai", "google_genai", "ollama", "deepseek"}:
            content = json.dumps(structured)
            if provider == "google_genai":
                response = genai_types.GenerateContentResponse.model_validate(
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": content}]},
                                "finishReason": "STOP",
                            }
                        ]
                    }
                )
            elif provider == "ollama":
                response = {
                    "message": {"role": "assistant", "content": content},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                response = ParsedChatCompletion[dict].model_validate(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gpt-test",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": content,
                                    "parsed": structured,
                                },
                            }
                        ],
                    }
                )
        else:
            if provider == "anthropic":
                response = AnthropicMessage.model_validate(
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-test",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_test",
                                "name": "ScopeDecision",
                                "input": structured,
                            }
                        ],
                        "stop_reason": "tool_use",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                )
            else:
                response = {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": f"{provider}-test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_test",
                                        "type": "function",
                                        "function": {
                                            "name": "ScopeDecision",
                                            "arguments": json.dumps(structured),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
        return response

    def _response_boundary(self, model, provider, response):
        """Inject SDK responses, including integrations whose clients are private, without bypassing model parsing."""
        # Native SDK injection requires the private clients exposed by these pinned integrations.
        # pylint: disable=protected-access
        if provider in {"openai", "deepseek"}:
            raw = SimpleNamespace(parse=lambda: response, headers={})
            return patch.object(model.root_client.chat.completions.with_raw_response, "parse", return_value=raw)
        if provider == "anthropic":
            raw = SimpleNamespace(parse=lambda: response)
            return patch.object(model._client.messages.with_raw_response, "create", return_value=raw)
        if provider == "google_genai":
            return patch.object(model.client.models, "generate_content", return_value=response)
        if provider == "ollama":
            return patch.object(model._client, "chat", return_value=iter([response]))
        return patch.object(model.client.chat, "send", return_value=response)

    def test_native_default_parsers_classify_valid_and_reject_malformed_provider_text(self):
        """Run actual native structured output and scope classification for all bundled providers."""
        configs = {
            "openai": ("openai:gpt-test", {"api_key": SENTINEL}),
            "anthropic": ("anthropic:claude-test", {"api_key": SENTINEL}),
            "google_genai": ("google_genai:gemini-test", {"google_api_key": SENTINEL, "vertexai": False}),
            "deepseek": ("deepseek:deepseek-test", {"api_key": SENTINEL}),
        }
        marker = f"provider-private-{SENTINEL}"
        for provider, (model_name, parameters) in configs.items():
            with self.subTest(provider=provider):
                model = self.build(model_name, parameters=parameters)
                classifier = bind_scope_decision(model)
                for allowed in (True, False):
                    with self._response_boundary(model, provider, self._sdk_response(provider, allowed)):
                        decision = classify_request(classifier, "Is this request in scope?")
                    self.assertEqual(decision, ScopeDecision(allowed=allowed))

                with (
                    self._response_boundary(model, provider, self._sdk_response(provider, marker)),
                    self.assertRaises(ImproperlyConfigured) as caught,
                ):
                    classify_request(classifier, "Is this request in scope?")
                error = caught.exception
                self.assertNotIn(marker, str(error))
                self.assertNotIn(marker, "".join(traceback.format_exception(error)))
                self.assertIsNone(error.__cause__)
                self.assertTrue(error.__context__ is None or error.__suppress_context__)

    def test_deepseek_json_output_failures_remain_safe_in_both_scope_modes(self):
        model = self.build(
            "deepseek:deepseek-flash",
            parameters={"api_key": SENTINEL, "extra_body": {"thinking": {"type": "enabled"}}},
        )
        classifier = bind_scope_decision(model)
        for content in ("", "   ", "not JSON", "{}", '{"allowed": "not-a-boolean"}'):
            for topic_switch_only in (False, True):
                with self.subTest(content=content, topic_switch_only=topic_switch_only):
                    response = self._sdk_response("deepseek", True)
                    response.choices[0].message.content = content
                    with (
                        self._response_boundary(model, "deepseek", response),
                        self.assertRaises(ImproperlyConfigured),
                    ):
                        classify_request(
                            classifier,
                            "Continue",
                            [HumanMessage("List Nautobot devices")],
                            topic_switch_only=topic_switch_only,
                        )
