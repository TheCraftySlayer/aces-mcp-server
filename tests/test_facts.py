"""ACES_Key_Facts: only signed-off rows count, and the facts file itself is well formed."""

import datetime as dt
import json

import pytest

import aces_facts

TODAY = dt.date(2026, 10, 29)


@pytest.fixture
def facts_file(tmp_path, monkeypatch):
    def write(rows, topics=("value freeze",)):
        path = tmp_path / "facts.json"
        path.write_text(json.dumps({"topics": list(topics), "facts": rows}), encoding="utf-8")
        monkeypatch.setattr(aces_facts, "FACTS_PATH", str(path))

    return write


def row(**fields):
    base = {"id": "r1", "topic": "value freeze", "fact": "A fact.", "source": "A source."}
    base.update(fields)
    return base


def test_reviewed_needs_a_name_and_a_date(facts_file):
    facts_file(
        [
            row(id="signed", status="reviewed", reviewed_by="A. Reviewer", last_reviewed="2026-10-28"),
            row(id="unsigned", status="reviewed", reviewed_by="", last_reviewed="2026-10-28"),
        ]
    )
    text = aces_facts.key_facts("value freeze", today=TODAY)
    reviewed, draft = text.split("DRAFT")
    assert "[signed]" in reviewed
    assert "[unsigned]" in draft


def test_open_items_are_flagged_not_answered(facts_file):
    facts_file([row(id="q", status="open", ask="ask a supervisor")])
    text = aces_facts.key_facts("value freeze", today=TODAY)
    assert "OPEN (not settled; don't state an answer)" in text
    assert "No reviewed facts for this topic yet" in text


def test_no_figure_for_an_unknown_tax_year(facts_file):
    facts_file([row(id="y2026", tax_year=2026, status="reviewed", reviewed_by="A", last_reviewed="2026-10-28")])
    text = aces_facts.key_facts("value freeze", tax_year=2027, today=TODAY)
    assert "No figure on file for tax year 2027. Don't give one." in text
    assert "[y2026" not in text


def test_old_reviews_are_marked_overdue(facts_file):
    facts_file([row(status="reviewed", reviewed_by="A", last_reviewed="2024-01-01")])
    assert "REVIEW OVERDUE" in aces_facts.key_facts("value freeze", today=TODAY)


def test_unknown_topic_lists_the_topics(facts_file):
    facts_file([row()], topics=("value freeze", "protest"))
    assert aces_facts.key_facts("weather", today=TODAY) == "Pick a topic: value freeze; protest."


def test_the_real_facts_file_is_well_formed():
    with open(aces_facts.FACTS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    topics = set(data["topics"])
    ids = [f["id"] for f in data["facts"]]
    assert len(ids) == len(set(ids)), "fact ids must be unique"
    for f in data["facts"]:
        assert f["topic"] in topics, f["id"]
        assert f.get("status") in {"reviewed", "draft", "open"}, f["id"]
        assert f.get("fact"), f["id"]
        if f["status"] == "reviewed":
            assert f.get("reviewed_by") and f.get("last_reviewed"), f"{f['id']} is reviewed but not signed"
            dt.date.fromisoformat(f["last_reviewed"])
