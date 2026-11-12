"""Pure trace-inspector projection rules."""

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from django.test import SimpleTestCase

from nautobot_agent.runtime.trace_inspector import project


class TraceInspectorProjectionTests(SimpleTestCase):
    """Test trace projection, payload decoding, and integrity repair."""

    def setUp(self):
        self.started = datetime(2025, 1, 1, tzinfo=UTC)
        self.trace = SimpleNamespace(
            request={"input": "overview"},
            status="ok",
            execution_label="OK",
            capture_state="partial",
            started_at=self.started,
            duration_seconds=2.5,
        )

    def row(self, *, run_id=None, parent=None, kind="tool", name="span", details=None):
        """Build a detached span summary with an optional anomalous parent."""
        return {
            "run_id": run_id or uuid.uuid4(),
            "parent_run_id": parent,
            "started_at": self.started,
            "kind": kind,
            "name": name,
            "status": "success",
            "duration_seconds": 0.1,
            "details": details or {},
        }

    def test_missing_parent_and_cycle_are_repaired_for_display(self):
        root, child, orphan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        context = project(
            self.trace,
            [
                self.row(run_id=root, parent=child),
                self.row(run_id=child, parent=root),
                self.row(run_id=orphan, parent=uuid.uuid4()),
            ],
            child,
            {
                "details": {"events": []},
                "event_counts": {},
                "input_data": None,
                "output_data": None,
                "error_data": None,
            },
        )
        rows = {row["run_id"]: row for row in context["trace_rows"]}
        self.assertIsNone(rows[root]["parent_run_id"])
        self.assertTrue(rows[root]["integrity_notice"])
        self.assertIsNone(rows[orphan]["parent_run_id"])
        self.assertTrue(rows[orphan]["integrity_notice"])

    def test_selected_payload_decoding(self):
        run_id = uuid.uuid4()
        summaries = [self.row(run_id=run_id)]
        selected = {
            "details": {"events": []},
            "event_counts": {},
            "input_data": None,
            "output_data": None,
            "error_data": None,
        }
        context = project(self.trace, summaries, run_id, selected)
        self.assertEqual(context["active_tab"], "metadata")
        self.assertEqual(
            context["payloads"],
            [
                {"kind": "input", "available": False, "preview": "null", "incomplete": False},
                {"kind": "output", "available": False, "preview": "null", "incomplete": False},
            ],
        )
        selected["output_data"] = ""
        context = project(self.trace, summaries, run_id, selected)
        self.assertEqual(context["active_tab"], "io")
        self.assertEqual(
            context["payloads"][1],
            {"kind": "output", "available": True, "preview": '""', "incomplete": False},
        )

        with self.assertRaises(LookupError):
            project(self.trace, summaries, uuid.uuid4(), selected)
        self.trace.request = "é" * 40_000
        overview = project(self.trace, [], None, None)
        encoded = json.dumps(self.trace.request, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(overview["payloads"][0]["preview"], encoded[:1_024])
        self.assertTrue(overview["payloads"][0]["incomplete"])
