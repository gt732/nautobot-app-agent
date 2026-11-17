"""Bounded chart input and main-agent-only rendering tool."""

import json
from collections.abc import Mapping
from math import isfinite
from typing import Annotated, Any, Literal

from langchain.tools import tool
from nautobot.core.ui.choices import EChartsTypeChoices
from nautobot.core.ui.echarts import EChartsBase
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from nautobot_agent.data.tool_io import configure_tool_errors

MAX_SERIES = 10
MAX_CATEGORIES = 100
MAX_PIE_SLICES = 50
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500
MAX_AXIS_LABEL_LENGTH = 100
MAX_SERIES_NAME_LENGTH = 100
MAX_CATEGORY_NAME_LENGTH = 100
MAX_CHART_BYTES = 48 * 1024


_Number = StrictInt | StrictFloat
_HexColor = Annotated[StrictStr, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


def _invalid_label(name: str, max_length: int) -> bool:
    return not name.strip() or len(name) > max_length


def _distinct_categories(data: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Return distinct category names across all series in first-seen order."""
    return list(dict.fromkeys(category for values in data.values() for category in values))


class _ThemeColor(BaseModel):
    """One safe palette color for both Nautobot themes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    light: _HexColor
    dark: _HexColor


class _ChartInput(BaseModel):
    """Closed, bounded data contract for a native Nautobot chart."""

    model_config = ConfigDict(extra="forbid", strict=True)
    _chart_config: dict[str, Any] = PrivateAttr()

    chart_type: Literal["bar", "line", "pie"]
    orientation: Literal["vertical", "horizontal"] = Field(
        default="vertical",
        description="Bar orientation; horizontal bars use y for categories and x for values. Bar charts only.",
    )
    stacked: StrictBool = Field(
        default=False,
        description="Stack raw bar values by same sign without normalization. Bar charts only.",
    )
    title: Annotated[StrictStr, Field(max_length=MAX_TITLE_LENGTH)]
    description: Annotated[StrictStr, Field(max_length=MAX_DESCRIPTION_LENGTH)] | None = None
    x_label: Annotated[StrictStr, Field(max_length=MAX_AXIS_LABEL_LENGTH)] | None = None
    y_label: Annotated[StrictStr, Field(max_length=MAX_AXIS_LABEL_LENGTH)] | None = None
    data: Annotated[
        dict[StrictStr, dict[StrictStr, _Number]],
        Field(description="For bar, line, and pie, map series names to category/value mappings."),
    ]
    theme_colors: list[_ThemeColor] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CATEGORIES,
        description=(
            "Optional ordered palette of light/dark #RRGGBB colors. Single-series unstacked bars use one color "
            "per category in input order; grouped bars and stacks use one color per series; lines use one per "
            "series; pies use one per slice. Colors repeat when shorter. Omit to use Nautobot colors."
        ),
    )

    @property
    def chart_config(self) -> dict[str, Any]:
        """Return the configuration already checked against the serialized payload budget."""
        return self._chart_config

    @model_validator(mode="after")
    def validate_chart(self):
        """Enforce chart input semantics and label rules."""
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if self.chart_type != EChartsTypeChoices.BAR and (self.orientation != "vertical" or self.stacked):
            raise ValueError("orientation and stacked controls are supported only for bar charts")
        if not self.data or any(not values for values in self.data.values()):
            raise ValueError("data and every series must be non-empty")
        if any(_invalid_label(name, MAX_SERIES_NAME_LENGTH) for name in self.data):
            raise ValueError("series names must be non-blank and within the length limit")
        if any(_invalid_label(category, MAX_CATEGORY_NAME_LENGTH) for category in _distinct_categories(self.data)):
            raise ValueError("category names must be non-blank and within the length limit")
        return self

    @model_validator(mode="after")
    def validate_bounds(self):
        """Enforce per-chart-type series and category limits and finite values."""
        categories = _distinct_categories(self.data)
        if self.chart_type == EChartsTypeChoices.PIE:
            if len(self.data) != 1:
                raise ValueError("pie charts require exactly one series")
            if len(next(iter(self.data.values()))) > MAX_PIE_SLICES:
                raise ValueError(f"pie charts support at most {MAX_PIE_SLICES} slices")
        elif len(self.data) > MAX_SERIES or len(categories) > MAX_CATEGORIES:
            raise ValueError(f"bar and line charts support at most {MAX_SERIES} series and {MAX_CATEGORIES} categories")
        if any(
            isinstance(value, float) and not isfinite(value)
            for values in self.data.values()
            for value in values.values()
        ):
            raise ValueError("chart values must be finite numbers")
        return self

    @model_validator(mode="after")
    def build_within_budget(self):
        """Build the chart configuration and enforce the shared serialized payload budget."""
        self._chart_config = _build_chart_config(self)
        serialized = json.dumps(
            {"arguments": self.model_dump(mode="json", exclude_unset=True), "config": self._chart_config},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(serialized.encode("utf-8")) > MAX_CHART_BYTES:
            raise ValueError(
                f"chart arguments and configuration exceed {MAX_CHART_BYTES} UTF-8 bytes; "
                "narrow or aggregate the input rather than dropping values or labels"
            )
        return self


_CHART_DATA_GUIDANCE = (
    "Provide a mapping from series name to category name to finite number; pie charts require exactly one series. "
    f"Bar and line charts support at most {MAX_SERIES} series and {MAX_CATEGORIES} categories; "
    f"pies support at most {MAX_PIE_SLICES} slices."
)
_THEME_COLORS_GUIDANCE = "Provide a list of color objects with six-digit light and dark hex colors."


def _build_chart_config(chart: _ChartInput) -> dict[str, Any]:
    """Build native options from validated input without repeating public validation."""
    categories = _distinct_categories(chart.data)
    data = {
        "x": categories,
        "series": [
            {"name": name, "data": [values.get(category) for category in categories]}
            for name, values in chart.data.items()
        ],
    }
    config = EChartsBase(
        chart_type=chart.chart_type,
        header=chart.title,
        description=chart.description or "",
        x_label=chart.x_label or "" if chart.chart_type != EChartsTypeChoices.PIE else "",
        y_label=chart.y_label or "" if chart.chart_type != EChartsTypeChoices.PIE else "",
        data=data,
        theme_colors=[color.model_dump() for color in chart.theme_colors] if chart.theme_colors is not None else None,
    ).get_config()
    if chart.chart_type == EChartsTypeChoices.BAR:
        if chart.orientation == "horizontal":
            category_axis = config["xAxis"]
            value_axis = config["yAxis"]
            category_axis["type"] = "value"
            category_axis.pop("data", None)
            value_axis["type"] = "category"
            value_axis["data"] = categories
            value_axis["inverse"] = True
        if chart.stacked:
            for series in config["series"]:
                series["stack"] = "raw"
                series["stackStrategy"] = "samesign"
                series["colorBy"] = "series"
        elif len(chart.data) == 1:
            config["series"][0]["colorBy"] = "data"
    return config


def build_chart_config(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Validate chart arguments and build an ordered chart option."""
    return _ChartInput.model_validate(arguments).chart_config


@tool(args_schema=_ChartInput)
def render_chart(**arguments: Any) -> str:
    """Display an explicitly requested chart from obtained numbers."""
    data = arguments["data"]
    category_count = len({category for values in data.values() for category in values})
    return (
        f"Displayed {arguments['chart_type']} chart '{arguments['title']}' "
        f"with {len(data)} series and {category_count} categories."
    )


configure_tool_errors(
    render_chart,
    location_guidance={
        "data": _CHART_DATA_GUIDANCE,
        "theme_colors": _THEME_COLORS_GUIDANCE,
    },
)
