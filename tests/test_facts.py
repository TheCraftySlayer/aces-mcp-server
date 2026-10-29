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
    assert (
        "OPEN (not settled. Don't state an answer, and leave out anything a specialist says on these points.)" in text
    )
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


def _signed(**fields):
    return row(status="reviewed", reviewed_by="A. Reviewer", last_reviewed="2026-10-28", **fields)


def test_a_current_cosign_is_shown(facts_file):
    r = _signed()
    r.update(cosigned_by="Topic Owner", cosigned="2026-10-29", cosign_fingerprint=aces_facts.fingerprint(r))
    facts_file([r])
    assert "Co-signed by Topic Owner, 2026-10-29." in aces_facts.key_facts("value freeze", today=TODAY)


def test_a_cosign_lapses_when_the_fact_changes(facts_file):
    r = _signed()
    r.update(cosigned_by="Topic Owner", cosigned="2026-10-29", cosign_fingerprint=aces_facts.fingerprint(r))
    r["fact"] = "A different fact."
    assert aces_facts.cosign_state(r) == "stale"
    facts_file([r])
    assert "Co-signed" not in aces_facts.key_facts("value freeze", today=TODAY)


def test_the_reviewer_cant_cosign_their_own_row():
    r = _signed()
    r.update(cosigned_by="a. reviewer", cosigned="2026-10-29", cosign_fingerprint=aces_facts.fingerprint(r))
    assert aces_facts.cosign_state(r) == "none"


def test_required_cosign_turns_unsigned_rows_into_drafts(facts_file, monkeypatch):
    monkeypatch.setattr(aces_facts, "REQUIRE_COSIGN", True)
    good = _signed(id="good")
    good.update(cosigned_by="Topic Owner", cosigned="2026-10-29", cosign_fingerprint=aces_facts.fingerprint(good))
    facts_file([good, _signed(id="alone")])
    reviewed, draft = aces_facts.key_facts("value freeze", today=TODAY).split("DRAFT")
    assert "[good]" in reviewed
    assert "[alone]" in draft


def test_cosign_fields_in_the_real_file_are_complete():
    with open(aces_facts.FACTS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    for f in data["facts"]:
        fields = [f.get("cosigned_by"), f.get("cosigned"), f.get("cosign_fingerprint")]
        assert all(fields) or not any(fields), f"{f['id']} has a partial co-signature"
