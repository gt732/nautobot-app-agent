"""Helpers for inspecting rendered HTML in view tests."""

from html.parser import HTMLParser


class _FormControls(HTMLParser):
    """Collect rendered form controls and select options."""

    def __init__(self, content):
        super().__init__()
        self.controls = {}
        self.options = {}
        self._select = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        """Record inputs, textareas, selects, and options."""
        attributes = dict(attrs)
        if tag in ("input", "textarea", "select") and attributes.get("name"):
            self.controls[attributes["name"]] = attributes
            if tag == "select":
                self._select = attributes["name"]
                self.options[self._select] = []
        elif tag == "option" and self._select:
            self.options[self._select].append((attributes.get("value"), "selected" in attributes))

    def handle_endtag(self, tag):
        """Leave the current select."""
        if tag == "select":
            self._select = None


class _Anchors(HTMLParser):
    """Collect rendered links, their attributes, and visible text."""

    def __init__(self, content):
        super().__init__()
        self.anchors = []
        self._open = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        """Start recording a link with a destination."""
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self._open = {"id": attributes.get("id"), "href": attributes["href"], "text": ""}
            self.anchors.append(self._open)

    def handle_data(self, data):
        """Append visible link text."""
        if self._open is not None:
            self._open["text"] += data

    def handle_endtag(self, tag):
        """Leave the current link."""
        if tag == "a":
            self._open = None


def controls(response):
    """Return rendered form controls and select options."""
    return _FormControls(response.content.decode())


def anchors(response):
    """Return rendered links."""
    return _Anchors(response.content.decode()).anchors


def hidden_values(response):
    """Return every hidden form control value."""
    return {
        name: attrs.get("value", "")
        for name, attrs in controls(response).controls.items()
        if attrs.get("type") == "hidden"
    }
