"""Render assistant markdown without automatic resource loads."""

import nh3
from django import template
from django.conf import settings
from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt
from nautobot.core import constants

register = template.Library()
_MARKDOWN = MarkdownIt("commonmark", {"breaks": True}).enable(["table", "strikethrough"])


@register.filter()
def render_agent_markdown(value):
    """Render chat-compatible Markdown with the native policy minus images."""
    html = nh3.clean(  # pylint: disable=no-member  # Rust extension, declared in nh3's type stubs.
        _MARKDOWN.render(value),
        tags=constants.HTML_ALLOWED_TAGS - {"img"},
        attributes=constants.HTML_ALLOWED_ATTRIBUTES,
        url_schemes=set(settings.ALLOWED_URL_SCHEMES),
    )
    return mark_safe(html)  # noqa: S308  # Sanitized with the native policy minus images.
