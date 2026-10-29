"""Build_Chart reads every value from a tool result already in the chat, never from the model."""

import pytest

import aces_charts

VOLUME_RESULT = """Office volume by week
3 row(s)

1. first_day: 2026-10-05 | open_days: 5 | calls: 900 | avg_calls_per_open_day: 180.0
2. first_day: 2026-10-12 | open_days: 5 | calls: 950 | avg_calls_per_open_day: 190.0
3. first_day: 2026-10-19 | open_days: 4 | calls: 760 | avg_calls_per_open_day: 190.0

Source: ACES call, visitor and deed marts."""


def conversation_with(result_text):
    return [
        {"role": "user", "content": "calls by week"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "ACES_Office_Volume", "input": {"grain": "week"}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": result_text}]},
    ]


def test_parse_tables_reads_rows_and_columns():
    tables = aces_charts.parse_tables(VOLUME_RESULT)
    assert len(tables) == 1
    assert tables[0]["columns"][:3] == ["first_day", "open_days", "calls"]
    assert len(tables[0]["rows"]) == 3


def test_chart_values_come_from_the_tool_result():
    sources = aces_charts.collect_sources(conversation_with(VOLUME_RESULT), {})
    spec, summary = aces_charts.build_chart(
        {"sourceTool": "ACES_Office_Volume", "x": "first_day", "y": ["avg_calls_per_open_day"], "title": "Calls"},
        sources,
    )
    assert spec["series"][0]["values"] == [180.0, 190.0, 190.0]
    assert summary


def test_chart_needs_a_source_in_the_conversation():
    with pytest.raises(aces_charts.ChartError):
        aces_charts.build_chart({"sourceTool": "ACES_Forecast", "x": "target_date", "y": ["calls"]}, [])


def test_non_numeric_column_is_refused():
    sources = aces_charts.collect_sources(conversation_with(VOLUME_RESULT), {})
    with pytest.raises(aces_charts.ChartError):
        aces_charts.build_chart({"sourceTool": "ACES_Office_Volume", "x": "calls", "y": ["first_day"]}, sources)
