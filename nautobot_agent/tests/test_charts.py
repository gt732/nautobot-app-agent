"""Behavioral tests for bounded chart validation and Nautobot option generation."""

import json
from math import inf, nan

from django.test import SimpleTestCase
from pydantic import ValidationError

from nautobot_agent.tools.render_chart import (
    MAX_AXIS_LABEL_LENGTH,
    MAX_CATEGORIES,
    MAX_CATEGORY_NAME_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_PIE_SLICES,
    MAX_SERIES,
    MAX_SERIES_NAME_LENGTH,
    MAX_TITLE_LENGTH,
    build_chart_config,
    render_chart,
)


class ChartBuilderTestCase(SimpleTestCase):
    """Chart options preserve input meaning and enforce the public data bounds."""

    def chart(self, **values):
        """Build a chart with a minimal valid input and the requested boundary override."""
        return build_chart_config({"chart_type": "bar", "title": "Inventory", "data": {"Devices": {"a": 1}}, **values})

    def test_builder_output(self):
        data = {"x": {"March": 3, "January": 1}, "series": {"February": 2, "March": 4}}
        for chart_type in ("bar", "line"):
            with self.subTest(chart_type=chart_type):
                config = self.chart(chart_type=chart_type, data=data)
                self.assertEqual(config["xAxis"]["data"], ["March", "January", "February"])
                self.assertEqual([item["name"] for item in config["series"]], ["x", "series"])
                self.assertEqual(config["series"][0]["data"], [3, 1, None])
                self.assertEqual(config["series"][1]["data"], [4, None, 2])
                self.assertEqual([item["type"] for item in config["series"]], [chart_type, chart_type])

        config = self.chart(chart_type="pie", data={"Status": {"Active": 4, "Planned": 2}})
        self.assertNotIn("xAxis", config)
        self.assertNotIn("yAxis", config)
        self.assertEqual(
            config["series"][0]["data"],
            [{"name": "Active", "value": 4}, {"name": "Planned", "value": 2}],
        )

    def test_horizontal_stacked_bar_semantics(self):
        config = self.chart(
            orientation="horizontal",
            stacked=True,
            x_label="Amount",
            y_label="Device",
            data={"first": {"A": 3, "B": -2}, "second": {"A": -4, "C": 5}},
        )
        self.assertEqual(config["xAxis"]["type"], "value")
        self.assertEqual(config["xAxis"]["name"], "Amount")
        self.assertEqual(config["yAxis"]["type"], "category")
        self.assertEqual(config["yAxis"]["name"], "Device")
        self.assertEqual(config["yAxis"]["data"], ["A", "B", "C"])
        self.assertTrue(config["yAxis"]["inverse"])
        self.assertEqual([series["data"] for series in config["series"]], [[3, -2, None], [-4, None, 5]])
        self.assertTrue(all(series["stack"] == "raw" for series in config["series"]))
        self.assertTrue(all(series["stackStrategy"] == "samesign" for series in config["series"]))
        self.assertTrue(all(series["colorBy"] == "series" for series in config["series"]))

    def test_combined_utf8_byte_budget(self):
        """A valid-count chart can exceed the independent compact UTF-8 payload limit."""
        categories = ["界" * 99 + chr(0x1000 + index) for index in range(17)]
        data = {f"Series {series}": dict.fromkeys(categories, series) for series in range(MAX_SERIES)}
        with self.assertRaises(ValidationError):
            self.chart(data=data)

    def test_non_bar_controls_are_rejected_and_stacked_is_strict(self):
        for values in (
            {"chart_type": "line", "orientation": "horizontal"},
            {"chart_type": "pie", "stacked": True},
            {"stacked": 1},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.chart(**values)

    def test_tool_result(self):
        result = render_chart.invoke({"chart_type": "bar", "title": "Inventory", "data": {"Devices": {"Up": 3}}})
        self.assertIsInstance(result, str)

        result = render_chart.invoke(
            {"chart_type": "pie", "title": "Inventory", "data": {"A": {"x": 1}, "B": {"x": 2}}}
        )
        observation = json.loads(result)
        self.assertEqual(observation["error"], "ValidationError")
        self.assertEqual(observation["details"][0]["loc"], [])
        self.assertEqual(observation["details"][0]["type"], "value_error")
        self.assertIn("guidance", observation["details"][0])

    def test_input_validation(self):
        limits = {
            "title": MAX_TITLE_LENGTH,
            "description": MAX_DESCRIPTION_LENGTH,
            "x_label": MAX_AXIS_LABEL_LENGTH,
            "y_label": MAX_AXIS_LABEL_LENGTH,
        }
        self.assertEqual(self.chart(description=None, x_label=None, y_label=None), self.chart())

        rejected = [
            {"data": {}},
            {"data": {"Devices": {}}},
            {"data": {f"Series {number}": {"Category": number} for number in range(MAX_SERIES + 1)}},
            {"data": {"Devices": {f"Category {number}": number for number in range(MAX_CATEGORIES + 1)}}},
            {"chart_type": "pie", "data": {"A": {"one": 1}, "B": {"two": 2}}},
            {"chart_type": "pie", "data": {"Status": {f"Slice {n}": n for n in range(MAX_PIE_SLICES + 1)}}},
            *({"data": {"Devices": {"Category": value}}} for value in (nan, inf, -inf, True, "1")),
            *({field: "x" * (limit + 1)} for field, limit in limits.items()),
            *({field: 123} for field in limits),
            {"title": "  "},
            {"data": {" ": {"Category": 1}}},
            {"data": {"n" * (MAX_SERIES_NAME_LENGTH + 1): {"Category": 1}}},
            {"data": {"Series": {" ": 1}}},
            {"data": {"Series": {"c" * (MAX_CATEGORY_NAME_LENGTH + 1): 1}}},
            {"chart_type": "heatmap"},
            {"chart_type": "treemap"},
            {"options": {"series": []}},
        ]
        for values in rejected:
            with self.subTest(rejected=values), self.assertRaises(ValidationError) as raised:
                self.chart(**values)
            field = next(iter(values))
            if field in limits and values[field] != "  ":
                self.assertEqual(raised.exception.errors()[0]["loc"], (field,))
