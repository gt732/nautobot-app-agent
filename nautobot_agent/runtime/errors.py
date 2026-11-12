"""Provider-neutral model failures with safe, actionable public messages."""

from langchain_core.exceptions import (
    ContextOverflowError,
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelInvalidRequestError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelRefusalError,
    ModelTimeoutError,
)

from nautobot_agent.runtime.budgets import RunBudgetExceeded

_MODEL_FAILURES = (
    (
        ModelNotFoundError,
        "model_not_found",
        "The configured language model is unavailable. Ask your administrator to check the model name "
        "and whether the configured provider account has access.",
    ),
    (
        ModelAuthenticationError,
        "model_authentication_failed",
        "The language model provider rejected the configured credentials. "
        "Ask your administrator to check the provider credentials.",
    ),
    (
        ModelPermissionDeniedError,
        "model_permission_denied",
        "The configured provider account does not have permission to use the language model. "
        "Ask your administrator to check its access.",
    ),
    (
        ModelRateLimitError,
        "model_rate_limited",
        "The language model provider's rate or usage limit was reached. Try again later. "
        "If this continues, ask your administrator to check the provider's usage limits and quota.",
    ),
    (
        ModelTimeoutError,
        "model_timeout",
        "The language model provider took too long to respond. Try again. "
        "If this continues, contact your administrator.",
    ),
    (
        ModelConnectionError,
        "model_connection_failed",
        "The language model provider could not be reached. Try again later. "
        "If this continues, ask your administrator to check connectivity to the provider.",
    ),
    (
        ModelAPIError,
        "model_provider_unavailable",
        "The language model provider encountered a server error. Try again later. "
        "If this continues, contact your administrator.",
    ),
    (
        ContextOverflowError,
        "model_context_exceeded",
        "This conversation exceeds the language model's context limit. "
        "Start a new chat with a shorter request. If this continues, contact your administrator.",
    ),
    (
        ModelRefusalError,
        "model_refused",
        "The language model provider declined this request. Rephrase the request or start a new chat. "
        "If this continues, ask your administrator to review the configured model's policies.",
    ),
    (
        ModelInvalidRequestError,
        "model_request_invalid",
        "The language model provider rejected the request. "
        "Ask your administrator to check the model configuration and supported features.",
    ),
)

MODEL_ERROR_MESSAGES = {
    **{code: message for _error_type, code, message in _MODEL_FAILURES},
    "model_context_limit_missing": (
        "The Agent language model has no valid input-context limit in its model metadata. "
        "Ask your administrator to set a positive profile.max_input_tokens value in the Agent LLM parameters."
    ),
    "run_tool_call_limit": (
        "This run reached its tool-call limit. Try a smaller request or ask your administrator to adjust the limit."
    ),
    "run_model_call_limit": (
        "This run reached its model-call limit. Try a smaller request or ask your administrator to adjust the limit."
    ),
}


class ModelFailure(Exception):
    """A recognized model failure containing only a stable code and safe message."""

    def __init__(self, code: str):
        """Resolve a stable category to its public message."""
        self.code = code
        super().__init__(MODEL_ERROR_MESSAGES[code])


def model_failure(exc: Exception) -> ModelFailure | None:
    """Classify supported model exceptions without inspecting provider-controlled text."""
    if isinstance(exc, ModelFailure):
        return exc
    if isinstance(exc, RunBudgetExceeded):
        return ModelFailure(exc.code)
    for error_type, code, _message in _MODEL_FAILURES:
        if isinstance(exc, error_type):
            return ModelFailure(code)
    return None
