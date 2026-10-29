"""sign_facts.py: signing, co-signing and the rules that stop a bad signature."""

import datetime as dt
import json

import pytest

import aces_facts
import sign_facts

DAY = dt.date(2026, 10, 30)


@pytest.fixture
def facts(tmp_path):
    path = tmp_path / "facts.json"
    rows = [
        {"id": "a", "topic": "value freeze", "fact": "Fact A.", "source": "S", "status": "draft"},
        {"id": "b", "topic": "value freeze", "fact": "Fact B.", "source": "S", "status": "open"},
    ]
    path.write_text(json.dumps({"topics": ["value freeze"], "facts": rows}), encoding="utf-8")

    def read():
        return {f["id"]: f for f in json.loads(path.read_text(encoding="utf-8"))["facts"]}

    return path, read


def test_sign_then_cosign(facts):
    path, read = facts
    assert sign_facts.main(["Reviewer One", "a"], path, DAY) == 0
    assert read()["a"]["reviewed_by"] == "Reviewer One"
    assert sign_facts.main(["--cosign", "Topic Owner", "a"], path, DAY) == 0
    row = read()["a"]
    assert row["cosigned_by"] == "Topic Owner" and aces_facts.cosign_state(row) == "cosigned"


def test_cosign_refusals_change_nothing(facts):
    path, read = facts
    before = path.read_text(encoding="utf-8")
    assert sign_facts.main(["--cosign", "Topic Owner", "a"], path, DAY) == 1  # not reviewed yet
    sign_facts.main(["Reviewer One", "a"], path, DAY)
    before = path.read_text(encoding="utf-8")
    assert sign_facts.main(["--cosign", "reviewer one", "a"], path, DAY) == 1  # same person
    assert sign_facts.main(["--cosign", "Topic Owner", "a", "nope"], path, DAY) == 1  # unknown id
    assert path.read_text(encoding="utf-8") == before


def test_open_rows_cant_be_signed(facts):
    path, read = facts
    assert sign_facts.main(["Reviewer One", "b"], path, DAY) == 1
    assert read()["b"]["status"] == "open"


def test_resigning_a_changed_row_clears_the_old_cosign(facts):
    path, read = facts
    sign_facts.main(["Reviewer One", "a"], path, DAY)
    sign_facts.main(["--cosign", "Topic Owner", "a"], path, DAY)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["facts"][0]["fact"] = "Fact A, corrected."
    path.write_text(json.dumps(data), encoding="utf-8")
    sign_facts.main(["Reviewer One", "a"], path, DAY)
    row = read()["a"]
    assert "cosigned_by" not in row and aces_facts.cosign_state(row) == "none"
