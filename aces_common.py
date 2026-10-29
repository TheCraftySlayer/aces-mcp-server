"""
Small helpers shared by the server modules: logging, time, hashing, text and JSON.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today_mountain_date():
    """Return today's date in America/Denver, falling back safely to UTC."""
    try:
        return datetime.now(ZoneInfo("America/Denver")).date()
    except ZoneInfoNotFoundError:  # no time zone data (tzdata missing on Windows)
        pass
    return datetime.now(UTC).date()


def _past_years_date_range(years: int = 10) -> tuple[str, str]:
    """Return YYYY-MM-DD date_from/date_to for a rolling lookback window."""
    today = _today_mountain_date()
    years = max(1, int(years or 10))
    try:
        start = today.replace(year=today.year - years)
    except ValueError:
        # Handles leap-day current dates.
        start = today.replace(year=today.year - years, month=2, day=28)
    return start.isoformat(), today.isoformat()


def _has_any_word_or_phrase(text: str, terms: list[str]) -> bool:
    """Match whole words/phrases so 'comp' does not match 'complete' or 'compliance'."""
    value = re.sub(r"\s+", " ", (text or "").lower()).strip()
    for term in terms:
        term_value = re.sub(r"\s+", " ", (term or "").lower()).strip()
        if not term_value:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(term_value) + r"(?![a-z0-9])"
        if re.search(pattern, value):
            return True
    return False


def _stable_hash(text: str) -> str:
    value = text or ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _log(message: str, **kwargs: Any) -> None:
    """One-line key=value log to stdout (App Service and the local server window both show it)."""
    details = " ".join(f"{key}={value}" for key, value in kwargs.items() if value is not None)
    print(f"[aces] {message}" + (f" {details}" if details else ""), flush=True)


def _error_preview(status_code: int, payload: Any, max_len: int = 1200) -> str | None:
    """
    Response bodies are logged only on errors. Successful CustomGPT responses echo
    staff prompts and answers, which can contain taxpayer details, so they stay
    out of the host's logs. _log() skips None values.
    """
    if status_code >= 400:
        return _safe_json_dumps(payload, max_len)
    return None


def _extract_label_block(prompt_text: str, label: str, stop_labels: list[str] | None = None) -> str:
    """Extract a labeled section from promptText."""
    text = prompt_text or ""
    stop_labels = stop_labels or []
    pattern = re.compile(re.escape(label) + r"\s*(.*)", re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    if not match:
        return ""

    block = match.group(1)
    for stop in stop_labels:
        stop_pattern = re.compile(r"\n\s*" + re.escape(stop), re.IGNORECASE)
        stop_match = stop_pattern.search(block)
        if stop_match:
            block = block[: stop_match.start()]
    return block.strip()


def _safe_json_dumps(value: Any, max_len: int = 3000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "...[truncated]"
    return text


async def _read_json_or_text(response: httpx.Response) -> Any:
    text = response.text or ""
    try:
        return response.json()
    except ValueError:
        return {"raw": text}
