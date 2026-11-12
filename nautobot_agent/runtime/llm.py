"""Chat-model construction from stored configuration and deployment credentials."""

import logging

from django.core.exceptions import ImproperlyConfigured, ValidationError
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from nautobot_agent.models import LLMConfiguration

logger = logging.getLogger("nautobot_agent.llm")


class DefaultLLMNotConfigured(ImproperlyConfigured):
    """No administrator-selected Agent model is available."""


def get_default_configuration() -> LLMConfiguration:
    """Return the single default configuration or raise a safe error."""
    default = LLMConfiguration.objects.filter(is_default=True).first()
    if default is None:
        raise DefaultLLMNotConfigured("No default LLM configuration is set; an administrator must select one.")
    return default


def get_scope_classifier_configuration() -> LLMConfiguration:
    """Return the dedicated classifier configuration, or the default one when none is assigned."""
    return LLMConfiguration.objects.filter(is_scope_classifier=True).first() or get_default_configuration()


def build_chat_model(config: LLMConfiguration) -> BaseChatModel:
    """Build the fixed chat model for a configuration; profile kwargs override identical parameters.

    Raises ImproperlyConfigured with static messages only; kwargs and exception bodies are never exposed or logged.
    """
    try:
        provider, model_name, kwargs = config.resolve_model_arguments()
    except ValidationError:
        raise ImproperlyConfigured("The LLM configuration is invalid.") from None

    try:
        return init_chat_model(model_name, model_provider=provider, configurable_fields=None, **kwargs)
    except ImportError:
        logger.error("LLM integration package unavailable (ImportError)")
        raise ImproperlyConfigured("The LLM provider integration package is not installed.") from None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("LLM model construction failed (%s)", type(exc).__name__)
        raise ImproperlyConfigured("The LLM model could not be constructed from its configuration.") from None
