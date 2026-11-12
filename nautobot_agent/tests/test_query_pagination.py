"""Signed query continuation expiry and fixed-lifetime behavior."""

from unittest.mock import patch

from django.core import signing
from django.test import SimpleTestCase
from langchain.tools import ToolException

from nautobot_agent.data.query_pagination import QueryContinuation


class QueryPaginationTests(SimpleTestCase):
    """Continuation expiry starts at signing and is retained across pages."""

    USER_ID = "user-7"
    FINGERPRINT = "query-fingerprint"
    POSITION = "00000000-0000-4000-8000-000000000001"
    SALT = "nautobot_agent.query_records.cursor.v1"

    def _token(self, strategy="pk", position=None, **overrides):
        payload = {
            "q": self.FINGERPRINT,
            "u": self.USER_ID,
            "p": position if position is not None else (self.POSITION if strategy == "pk" else 17),
            "e": 4_600,
        }
        payload.update(overrides)
        return signing.dumps(payload, salt=self.SALT, compress=True)

    def _bind(self, token, *, strategy="pk", now=1_000, **overrides):
        with patch("nautobot_agent.data.query_pagination.time.time", return_value=now):
            return QueryContinuation.bind(
                token,
                user_id=self.USER_ID,
                fingerprint=self.FINGERPRINT,
                strategy=strategy,
                **overrides,
            )

    def test_expiry_boundary_and_resumed_signing_preserve_fixed_expiry(self):
        continuation = self._bind(self._token(), now=4_599)
        next_token = continuation.next_cursor("00000000-0000-4000-8000-000000000002")
        payload = signing.loads(next_token, salt=self.SALT)
        self.assertEqual(payload["e"], 4_600)
        with self.assertRaisesRegex(ToolException, "CursorExpired"):
            self._bind(next_token, now=4_600)
