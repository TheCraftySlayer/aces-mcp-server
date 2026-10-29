"""
Charts for the A.C.E.S. chat.

Build_Chart is a chat-only tool. It is offered to the chat model next to this
server's MCP tools, but it is not an MCP tool, so Copilot Studio and CustomGPT
don't see it (they have no page to draw a chart on).

The rule that matters: the model never supplies numbers. It names a tool result
that is already in the conversation (for example the latest ACES_Office_Volume
result) and the columns to plot. The server reads the values out of that result
text, so a chart can't disagree with the data it came from. If the result was
cut off, has no such column, or the column isn't numeric, the call fails and
says why instead of drawing something partial.

Results are read in the row format that aces_sql_tools._format_rows writes:

    <title>
    <N> row(s)[ (limit reached)]

    1. col: value | col: value | ...
    2. ...

ACES_Daily_Briefing has several of these sections; the `table` argument picks
one by words from its title.
"""

import json
import math
import re
from typing import Any

CHART_TOOL_NAME = "Build_Chart"

MAX_SERIES = 6
MAX_LINE_POINTS = 200
MAX_BAR_POINTS = 40
MAX_TITLE_CHARS = 120
MAX_LABEL_CHARS = 60

# Tools whose results are forecasts, and the column-name hints for when a forecast was made
# (the same hints aces_sql_tools uses for the STALE FORECAST check).
_FORECAST_SOURCES = ("ACES_Forecast", "ACES_Daily_Briefing", "ACES_Season_Outlook")
_FORECAST_MADE_HINTS = ("as_of", "generated", "issued", "created", "stored", "run_at", "run_time", "made")
_CUSTOMER_FACING_NOTE = "Calls are customer-facing calls, a smaller count than total inbound calls."

# Columns that mean a period's source data is incomplete when above 0, the
# reason, and the word a charted column must contain for it to matter ("" = any).
_INCOMPLETE_COLUMNS = {
    "open_days_missing_calls": (
        "missing call data for some open days. Call totals there read low; averages use only days with data",
        "call",
    ),
    "open_days_missing_visits": (
        "missing visit data for some open days. Visit totals there read low; averages use only days with data",
        "visit",
    ),
    "days_missing_from_calendar": (
        "missing days in the office calendar, so open days and averages there may be off",
        "",
    ),
    "open_days_without_report": ("missing deed reports for some open days, so deeds there read low", ""),
    "days_needing_review": ("deed days waiting for review that aren't counted yet", ""),
}

CHART_TOOL: dict[str, Any] = {
    "name": CHART_TOOL_NAME,
    "description": (
        "Draws a line or bar chart in the chat from a data tool result that is already in this conversation. "
        "Call the data tool first (for example ACES_Office_Volume, ACES_Forecast, ACES_Deed_Volume), then call "
        "this with that tool's name and the exact column names from its result. The server reads every value "
        "from that result; you never pass numbers. One chart uses one result, so it can't put two tools' results "
        "(such as actual calls and a forecast) on the same chart; draw two charts instead. Use avg_ columns to compare periods that have different "
        "numbers of open days. Line charts suit trends over time (up to 200 points); bar charts suit up to 40 "
        "categories. For a forecast, put the forecast column in y and its 80% low and high columns in "
        "rangeLow and rangeHigh; if the result also has the 95% columns, the chart opens on the 80% range and adds a "
        "button to switch to 95%, so draw it once. For season dates (ACES_Forecast or ACES_Season_Outlook), chart season_calls with "
        "season_calls_low and season_calls_high as the range. Returns a short summary and any data notes, which you must pass along."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sourceTool": {
                "type": "string",
                "description": "Name of the tool whose result to chart, e.g. ACES_Office_Volume.",
            },
            "x": {
                "type": "string",
                "description": "Column for the horizontal axis, usually first_day or a date column.",
            },
            "y": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_SERIES,
                "description": "Numeric columns to plot, one line or bar per column (1 to 6).",
            },
            "title": {"type": "string", "description": "Short plain title, e.g. 'Calls per open day, by week'."},
            "chartType": {"type": "string", "enum": ["line", "bar"], "description": "line (default) or bar."},
            "seriesLabels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional plain legend labels, same order as y.",
            },
            "yLabel": {"type": "string", "description": "Optional axis label, e.g. 'Calls per open day'."},
            "rangeLow": {
                "type": "string",
                "description": "Optional column for the low edge of a shaded range around the first y series.",
            },
            "rangeHigh": {"type": "string", "description": "Optional column for the high edge of that range."},
            "rangeLabel": {
                "type": "string",
                "description": "Plain legend label for the range, e.g. '80% forecast range'.",
            },
            "table": {
                "type": "string",
                "description": "Only when the result has several sections (ACES_Daily_Briefing): words from the section title.",
            },
            "resultsBack": {
                "type": "integer",
                "minimum": 1,
                "description": "1 (default) charts the most recent result from sourceTool, 2 the one before it, and so on.",
            },
        },
        "required": ["sourceTool", "x", "y", "title"],
    },
}


class ChartError(Exception):
    pass


# ---------------------------------------------------------------------------
# Reading tool results
# ---------------------------------------------------------------------------

_ROWS_HEADER = re.compile(r"^(\d+) row\(s\)( \(limit reached\))?\s*$")
_ROW = re.compile(r"^(\d+)\.\s(.*)$")


def _split_cells(body: str) -> list[tuple[str, str]]:
    """'a: 1 | b: x' -> [('a', '1'), ('b', 'x')]. A piece with no 'name:' joins the previous value."""
    cells: list[tuple[str, str]] = []
    for piece in body.split(" | "):
        name, sep, value = piece.partition(":")
        name = name.strip()
        if sep and name and re.fullmatch(r"[A-Za-z0-9_ %./#()-]{1,80}", name):
            cells.append((name, value.strip()))
        elif cells:
            last_name, last_value = cells[-1]
            cells[-1] = (last_name, f"{last_value} | {piece}".strip())
        else:
            raise ValueError("row doesn't start with a column name")
    return cells


def parse_tables(text: str) -> list[dict[str, Any]]:
    """Every row table in a tool result: title, expected count, columns, rows (dicts keyed by lowercase column)."""
    lines = str(text or "").splitlines()
    tables: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        header = _ROWS_HEADER.match(lines[i].strip())
        if not header:
            i += 1
            continue
        title = ""
        for k in range(i - 1, -1, -1):
            if lines[k].strip():
                title = lines[k].strip().strip("= ").strip()
                break
        expected = int(header.group(1))
        limit_reached = bool(header.group(2))
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        columns: list[str] = []
        rows: list[dict[str, str]] = []
        while j < len(lines):
            m = _ROW.match(lines[j].rstrip())
            if not m or int(m.group(1)) != len(rows) + 1:
                break
            try:
                cells = _split_cells(m.group(2))
            except ValueError:
                raise ChartError(f"Couldn't read row {len(rows) + 1} of '{title}'.")
            names = [c[0] for c in cells]
            if not columns:
                columns = names
            elif [n.lower() for n in names] != [c.lower() for c in columns]:
                raise ChartError(
                    f"Row {len(rows) + 1} of '{title}' has different columns from row 1, so it can't be charted safely."
                )
            rows.append({n.lower(): v for n, v in cells})
            j += 1
        tables.append(
            {"title": title, "expected": expected, "limit_reached": limit_reached, "columns": columns, "rows": rows}
        )
        i = j
    return tables


def _number(value: str) -> float | None:
    """Numeric cell to float; '' or 'None' -> None; anything else non-numeric -> ValueError."""
    s = str(value or "").strip().replace(",", "")
    if s in ("", "None", "null"):
        return None
    if s.endswith("%"):
        s = s[:-1]
    if s.startswith("$"):
        s = s[1:]
    n = float(s)
    if math.isnan(n) or math.isinf(n):
        return None
    return n


def _x_value(value: str) -> str:
    s = str(value or "").strip()
    return re.sub(r"^(\d{4}-\d{2}-\d{2})T00:00:00(\.0+)?$", r"\1", s)


def _pretty(column: str) -> str:
    s = column.replace("_", " ").strip()
    return (s[:1].upper() + s[1:])[:MAX_LABEL_CHARS]


def _clip(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# ---------------------------------------------------------------------------
# Building a chart
# ---------------------------------------------------------------------------


def collect_sources(conversation: list[dict[str, Any]], full_text: dict[str, str]) -> list[dict[str, Any]]:
    """
    Tool results in conversation order: name, input, text, is_error.
    full_text holds untruncated results from the current request, keyed by tool_use id.
    """
    uses: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    for message in conversation:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = block.get("type")
            if kind == "tool_use":
                uses[block.get("id")] = block
            elif kind == "tool_result":
                use = uses.get(block.get("tool_use_id"))
                if not use:
                    continue
                sources.append(
                    {
                        "name": use.get("name", ""),
                        "input": use.get("input") or {},
                        "text": full_text.get(block.get("tool_use_id"), block.get("content") or ""),
                        "is_error": bool(block.get("is_error")),
                    }
                )
    return sources


def _pick_table(tables: list[dict[str, Any]], needed: list[str], hint: str, source_name: str) -> dict[str, Any]:
    with_rows = [t for t in tables if t["rows"]]
    if not with_rows:
        raise ChartError(f"The {source_name} result has no rows to chart.")
    if hint:
        words = [w for w in re.split(r"\W+", hint.lower()) if w]
        hinted = [t for t in with_rows if all(w in t["title"].lower() for w in words)]
        if not hinted:
            titles = "; ".join(t["title"] for t in with_rows)
            raise ChartError(f"No section of the {source_name} result matches '{hint}'. Sections: {titles}.")
        with_rows = hinted
    fits = [t for t in with_rows if all(c in t["rows"][0] for c in needed)]
    if not fits:
        cols = sorted({c for t in with_rows for c in t["columns"]}, key=str.lower)
        missing = sorted({c for t in with_rows for c in needed if c not in t["rows"][0]})
        raise ChartError(
            f"The {source_name} result has no column named {', '.join(missing)}. "
            f"Columns it has: {', '.join(cols[:60])}."
        )
    return fits[0]


def build_chart(args: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """Returns (spec for the browser, summary for the model). Raises ChartError with a plain reason."""
    source_name = _clip(args.get("sourceTool"), 100)
    if not source_name:
        raise ChartError("Say which tool's result to chart (sourceTool).")
    if source_name == CHART_TOOL_NAME:
        raise ChartError("Build_Chart can't chart its own output. Name the data tool instead.")

    x_col = _clip(args.get("x"), 100).lower()
    y_raw = args.get("y")
    if isinstance(y_raw, str):
        y_raw = [y_raw]
    y_cols = [_clip(c, 100).lower() for c in (y_raw or []) if _clip(c, 100)]
    if not x_col or not y_cols:
        raise ChartError("Give the x column and at least one y column.")
    if len(y_cols) > MAX_SERIES:
        raise ChartError(
            f"Up to {MAX_SERIES} series fit on one chart; {len(y_cols)} were asked for. Split it into two charts."
        )
    if len(set(y_cols)) != len(y_cols):
        raise ChartError("The same y column is listed twice.")

    chart_type = _clip(args.get("chartType") or "line", 10).lower()
    if chart_type not in ("line", "bar"):
        raise ChartError("chartType must be line or bar.")
    low_col = _clip(args.get("rangeLow"), 100).lower()
    high_col = _clip(args.get("rangeHigh"), 100).lower()
    if bool(low_col) != bool(high_col):
        raise ChartError("A shaded range needs both rangeLow and rangeHigh.")

    try:
        back = int(args.get("resultsBack") or 1)
    except (TypeError, ValueError):
        back = 1
    back = max(1, back)

    matches = [s for s in sources if s["name"] == source_name and not s["is_error"]]
    if not matches:
        raise ChartError(
            f"There's no {source_name} result in this conversation to chart. Call {source_name} first, then Build_Chart."
        )
    if back > len(matches):
        raise ChartError(f"There are only {len(matches)} {source_name} result(s) in this conversation.")
    source = matches[-back]

    tables = parse_tables(source["text"])
    if not tables:
        raise ChartError(f"The {source_name} result isn't in a row format that can be charted.")
    needed = [x_col] + y_cols + ([low_col, high_col] if low_col else [])
    table = _pick_table(tables, needed, _clip(args.get("table"), 100), source_name)
    rows = table["rows"]

    if len(rows) < table["expected"]:
        raise ChartError(
            f"The {source_name} result was cut off at {len(rows)} of {table['expected']} rows, so a chart would be "
            "incomplete. Ask for a shorter date range or a coarser grain (week or month), then chart that."
        )
    limit = MAX_BAR_POINTS if chart_type == "bar" else MAX_LINE_POINTS
    if len(rows) > limit:
        hint = " Use a line chart, or a coarser grain." if chart_type == "bar" else " Use a coarser grain."
        raise ChartError(
            f"{len(rows)} points is more than a {chart_type} chart can show clearly (limit {limit}).{hint}"
        )

    def column_values(col: str) -> list[float | None]:
        out: list[float | None] = []
        for n, row in enumerate(rows, start=1):
            try:
                out.append(_number(row.get(col, "")))
            except ValueError:
                raise ChartError(f"{col} isn't a numeric column (row {n} has '{_clip(row.get(col), 40)}').")
        return out

    original = {c.lower(): c for c in table["columns"]}
    labels_in = args.get("seriesLabels") if isinstance(args.get("seriesLabels"), list) else []
    series = []
    for k, col in enumerate(y_cols):
        values = column_values(col)
        if all(v is None for v in values):
            raise ChartError(f"{original.get(col, col)} has no numbers in this result.")
        label = (
            _clip(labels_in[k], MAX_LABEL_CHARS)
            if k < len(labels_in) and _clip(labels_in[k], MAX_LABEL_CHARS)
            else _pretty(original.get(col, col))
        )
        series.append({"label": label, "column": original.get(col, col), "values": values})

    band = None
    band_alt = None
    opened_on_80 = False
    if low_col:
        if chart_type != "line":
            raise ChartError("A shaded range only works on a line chart.")
        low, high = column_values(low_col), column_values(high_col)
        for n, (lo, hi) in enumerate(zip(low, high), start=1):
            if lo is not None and hi is not None and lo > hi:
                raise ChartError(f"Row {n} has {low_col} above {high_col}; check the column order.")
        band = {"low": low, "high": high, "label": _clip(args.get("rangeLabel"), MAX_LABEL_CHARS) or "Range"}
        band_alt = _alternate_band(rows, low_col, high_col, band["label"], column_values)
        # Always open on the 80% range, the one answers quote; the 95% range is one click away.
        if band_alt and "95" in low_col and "95" in high_col:
            band, band_alt = band_alt, band
            opened_on_80 = True

    notes: list[str] = []
    if "partial_days" in rows[0]:
        partial = [r for r in rows if (_safe_number(r.get("partial_days")) or 0) > 0]
        # Averages already leave today out, so only totals need the note.
        if partial and any(not c.startswith("avg_") for c in y_cols):
            when = _friendly_x(partial[-1].get(x_col, ""))
            notes.append(f"{when} includes today, which is still loading, so it reads low.")
    for col, (meaning, applies_to) in _INCOMPLETE_COLUMNS.items():
        if applies_to and not any(applies_to in c for c in y_cols):
            continue
        if col in rows[0]:
            count = sum(1 for r in rows if (_safe_number(r.get(col)) or 0) > 0)
            if count:
                notes.append(f"{count} {'period has' if count == 1 else 'periods have'} {meaning}.")
    if "DEED DATA GAP" in source["text"] and source_name in ("ACES_Deed_Volume", "ACES_Daily_Briefing"):
        notes.append("Deed data has a loading gap: the most recent deed figures are incomplete, not zero.")
    stale = re.search(
        r"STALE FORECAST: the newest forecast here was made on (\d{4}-\d{2}-\d{2}), (\d+) days ago", source["text"]
    )
    if stale and source_name in ("ACES_Forecast", "ACES_Daily_Briefing"):
        notes.append(
            f"This forecast was made {_friendly_x(stale.group(1))} ({stale.group(2)} days ago) "
            "and hasn't been updated since, so it's out of date."
        )
    if table["limit_reached"]:
        notes.append("The data tool stopped at its row limit, so later periods aren't shown.")

    # Captions: shown under the chart and on a saved image, which travels without the chat,
    # but not data problems, so the model isn't asked to repeat them.
    captions: list[str] = []
    if source_name in _FORECAST_SOURCES and not stale:
        made = _newest_made_date(rows)
        if made:
            captions.append(f"Forecast made {_long_date(made)}.")
    if any("forecast_basis" in c for c in y_cols) or (
        source_name in _FORECAST_SOURCES
        and any(c == "calls" or c.startswith("calls_") or c.startswith("season_calls") for c in y_cols)
    ):
        captions.append(_CUSTOMER_FACING_NOTE)

    # Periods when the office was closed the whole time (weekends, holidays) are shaded,
    # so dips read as closures, not demand. Blank open_days means the calendar isn't
    # loaded for that period, so it isn't marked either way.
    closed: list[bool] | None = None
    if "open_days" in rows[0]:
        marks = [_safe_number(r.get("open_days")) == 0 for r in rows]
        if any(marks) and not all(marks):
            closed = marks

    title = _clip(args.get("title"), MAX_TITLE_CHARS) or table["title"][:MAX_TITLE_CHARS]

    # A one-row result (a summary such as ACES_Forecast_Accuracy) has nothing to plot
    # along x. Chart its measures as the bars instead: one labeled bar per column.
    by_measure = len(rows) == 1 and len(series) > 1 and band is None
    if by_measure:
        chart_type = "bar"
        x_spec = {"label": "Measure", "values": [s["label"] for s in series]}
        series = [
            {
                "label": _clip(args.get("yLabel"), MAX_LABEL_CHARS) or "Value",
                "column": ", ".join(s["column"] for s in series),
                "values": [s["values"][0] for s in series],
            }
        ]
        closed = None
    else:
        x_spec = {"label": _pretty(original.get(x_col, x_col)), "values": [_x_value(r.get(x_col, "")) for r in rows]}

    spec = {
        "v": 1,
        "type": chart_type,
        "title": title,
        "x": x_spec,
        "byMeasure": by_measure,
        "yLabel": _clip(args.get("yLabel"), MAX_LABEL_CHARS),
        "series": series,
        "band": band,
        "bandAlt": band_alt,
        "closed": closed,
        "notes": notes + captions,
        "source": (f"{table['title']} ({source_name})" if table["title"] else source_name)[:240],
    }

    if by_measure:
        summary = (
            f"Chart shown to the user: '{title}', a bar chart with one bar per measure "
            f"({', '.join(x_spec['values'])}), read directly from the single-row {source_name} result ({table['title']})."
        )
    else:
        summary = (
            f"Chart shown to the user: '{title}', a {chart_type} chart of "
            f"{', '.join(s['column'] for s in series)} by {spec['x']['label'].lower()}, {len(rows)} points, "
            f"read directly from the {source_name} result ({table['title']})."
        )
    if band_alt:
        summary += (
            f" A button on the chart switches the shaded range between the {band['label']} and the "
            f"{band_alt['label']}; the user can use it, so don't redraw the chart for the other range."
        )
        if opened_on_80:
            summary += f" The chart opens on the {band['label']}; to see the 95% range, the user clicks Show 95% range."
    if closed:
        summary += (
            f" Days or periods when the office was closed ({sum(closed)}) are shaded on the chart and labeled Closed."
        )
    if notes:
        summary += " Data notes to pass along: " + " ".join(notes)
    summary += " The chart is on screen; give the takeaway in a sentence or two rather than repeating its numbers."
    return spec, summary


# Forecast ranges that can be swapped on the chart: calls_low80/calls_high80 <-> calls_low95/calls_high95.
_RANGE_LEVELS = ("80", "95")


def _alternate_band(rows, low_col, high_col, label, column_values) -> dict[str, Any] | None:
    """The other forecast range (95% for an 80% band, or 80% for a 95% band) when the result has it, else None."""
    for have, other in (_RANGE_LEVELS, _RANGE_LEVELS[::-1]):
        if have in low_col and have in high_col:
            alt_low, alt_high = low_col.replace(have, other), high_col.replace(have, other)
            break
    else:
        return None
    if alt_low not in rows[0] or alt_high not in rows[0]:
        return None
    try:
        low, high = column_values(alt_low), column_values(alt_high)
    except ChartError:
        return None
    if all(v is None for v in low) or any(lo is not None and hi is not None and lo > hi for lo, hi in zip(low, high)):
        return None
    alt_label = label.replace(have + "%", other + "%") if have + "%" in label else f"{other}% range"
    return {"low": low, "high": high, "label": alt_label}


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _newest_made_date(rows: list[dict[str, str]]) -> str:
    """Newest YYYY-MM-DD in a column that says when the forecast was made (as_of_date and the like), or ""."""
    best = ""
    for col in rows[0]:
        if not any(hint in col for hint in _FORECAST_MADE_HINTS):
            continue
        for r in rows:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", str(r.get(col, "")).strip())
            if m and m.group(1) > best:
                best = m.group(1)
    return best


def _long_date(value: str) -> str:
    """'2026-10-28' -> 'Oct 28, 2026'."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not m or not 1 <= int(m.group(2)) <= 12:
        return value
    return f"{_MONTHS[int(m.group(2)) - 1]} {int(m.group(3))}, {m.group(1)}"


def _friendly_x(value: Any) -> str:
    """'2026-10-15' -> 'Oct 15'; anything else as given (for note text)."""
    s = _x_value(value)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{_MONTHS[int(m.group(2)) - 1]} {int(m.group(3))}"
    return s or "The latest period"


def _safe_number(value: Any) -> float | None:
    try:
        return _number(value)
    except (TypeError, ValueError):
        return None


def chart_result_content(spec: dict[str, Any], summary: str) -> str:
    """What gets stored as the tool result: the browser redraws the chart from it after a reload."""
    return json.dumps({"summary": summary, "chart": spec}, ensure_ascii=False, separators=(",", ":"))
