"""The specialist answer cache: answers expire, old entries are dropped, and writes are atomic."""

import json
import time

import pytest

import server

QUESTION = "What is the statute citation for the value freeze?"


def _stamp(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))


@pytest.fixture
def cache(monkeypatch, tmp_path):
    """A private, empty task cache in a temp folder."""
    monkeypatch.setattr(server, "TASK_CACHE", {})
    monkeypatch.setattr(server, "TASK_CACHE_FILE", str(tmp_path / "task_cache.json"))
    return server.TASK_CACHE


def _answered(cache, minutes_ago: float, question: str = QUESTION) -> dict:
    key = server._task_cache_key("37400", "Compliance_Expert", question)
    cache[key] = {
        "project_id": "37400",
        "tool_name": "Compliance_Expert",
        "task_id": "111",
        "status": "completed",
        "dedupe_hash": server._stable_hash(server._canonical_prompt_for_cache("Compliance_Expert", question)),
        "created_at": _stamp(minutes_ago + 1),
        "updated_at": _stamp(minutes_ago),
        "answered_at": _stamp(minutes_ago),
        "last_answer": "Section 7-36-21.3 NMSA 1978.",
    }
    return cache[key]


def test_a_recent_answer_is_reused(cache):
    entry = _answered(cache, minutes_ago=60)
    assert server._find_existing_prompt_task("37400", "Compliance_Expert", QUESTION) is entry


def test_an_answer_older_than_the_window_is_not_reused(cache, monkeypatch):
    monkeypatch.setattr(server, "ANSWER_CACHE_HOURS", 24)
    _answered(cache, minutes_ago=25 * 60)
    assert server._find_existing_prompt_task("37400", "Compliance_Expert", QUESTION) is None


def test_zero_hours_turns_reuse_off(cache, monkeypatch):
    monkeypatch.setattr(server, "ANSWER_CACHE_HOURS", 0)
    _answered(cache, minutes_ago=1)
    assert server._find_existing_prompt_task("37400", "Compliance_Expert", QUESTION) is None


def test_a_running_task_is_reused_only_while_it_can_finish(cache):
    key = server._task_cache_key("37400", "Compliance_Expert", QUESTION)
    cache[key] = {
        "project_id": "37400",
        "tool_name": "Compliance_Expert",
        "task_id": "222",
        "status": "still_running",
        "created_at": _stamp(2),
        "updated_at": _stamp(1),
    }
    assert server._find_existing_prompt_task("37400", "Compliance_Expert", QUESTION) is cache[key]
    cache[key]["created_at"] = _stamp(server.TASK_MAX_MINUTES + 1)
    assert server._find_existing_prompt_task("37400", "Compliance_Expert", QUESTION) is None


def test_saving_drops_old_entries_and_leaves_no_temp_file(cache, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "TASK_CACHE_KEEP_DAYS", 7)
    _answered(cache, minutes_ago=60, question="recent question")
    _answered(cache, minutes_ago=8 * 24 * 60, question="old question")
    cache["no-time"] = {"task_id": "333"}
    server._save_task_cache(cache)
    saved = json.loads((tmp_path / "task_cache.json").read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert next(iter(saved.values()))["last_answer"]
    assert [p.name for p in tmp_path.iterdir()] == ["task_cache.json"]


def test_a_failed_write_keeps_the_previous_file(cache, monkeypatch, tmp_path):
    _answered(cache, minutes_ago=1)
    server._save_task_cache(cache)
    before = (tmp_path / "task_cache.json").read_text(encoding="utf-8")
    cache["bad"] = {"updated_at": _stamp(0), "value": object()}  # not JSON-serializable
    server._save_task_cache(cache)
    assert (tmp_path / "task_cache.json").read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["task_cache.json"]
