---
name: "nautobot-charts"
description: "Use only when the user explicitly asks to chart, graph, plot, or visualize numeric Nautobot data."
---
# Nautobot charts

- Use `render_chart` for numeric charts only after an explicit user request. Structural diagrams, including network/topology and flow diagrams, belong to `nautobot-mermaid`. Obtain values through permission-aware query tools and pass exact returned numbers; never estimate, infer, or fabricate values.
- Supported families are bars, lines, and pies. Bars and lines accept at most 10 series and 100 categories as `{series: {category: value}}`; pies accept one series and at most 50 slices. Keep original label order. Missing bar/line values remain gaps, not zero.
- Bars default to vertical and grouped. Use `orientation="horizontal"` for long labels and `stacked=true` for raw contributions; these compose, apply only to bars, and stack positive and negative values separately without normalization. Axis labels name physical axes.
- Optional `theme_colors` uses ordered light/dark six-digit hex color pairs. Single-series unstacked bars color by category; grouped bars, stacks, and lines color by series; pies color by slice. Omit it for Nautobot defaults; short palettes repeat.
- Paginated/grouped results may be partial. Follow signed continuation pages or narrow/aggregate within existing query limits before charting; never present a truncated page as a complete inventory. Do not turn permission-masked relations into categories or imply chart identities are Nautobot object grants. Distinguish duplicate object labels using available real identifiers when preparing categories.
- Successful arguments and generated options share a 48 KiB compact UTF-8 limit. Narrow or aggregate large results rather than dropping values or labels. If validation fails, use its repair guidance while retaining the user's requested conditions.
- After success, summarize briefly without repeating chart data as a table. Heatmaps, treemaps, scatter, candlestick, radar, boxplot, sunburst, and mixed charts are unsupported; offer the closest supported chart family rather than arbitrary ECharts options.
