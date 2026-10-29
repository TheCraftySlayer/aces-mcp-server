"""
Reviewed key facts for A.C.E.S.

ACES_Key_Facts returns figures people have signed off on (limits, deadlines,
exemption amounts, citations, office contact details) from aces_facts.json.
It is the check on specialist answers that doesn't depend on another chatbot:
values come from a reviewed file, and every change to that file is a git commit.

Row status in aces_facts.json:
  reviewed  signed off: reviewed_by and last_reviewed are filled in. Authoritative.
  draft     sourced but not signed off. Listed, but not used as a check.
  open      a question nobody has settled. The chat says so instead of answering.
"""

import datetime as dt
import json
import os

from aces_common import _log

FACTS_PATH = os.getenv(
    "ACES_FACTS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "aces_facts.json"),
)
REVIEW_MAX_DAYS = int(os.getenv("ACES_FACTS_REVIEW_DAYS", "365"))

DESCRIPTION = (
    "Read-only. Returns staff-reviewed key facts (dollar limits, deadlines, exemption amounts, statute "
    "citations, office contact details) from a reviewed file, not a chatbot. Call it first, before any "
    "specialist, for any figure, deadline or citation. If a REVIEWED fact fully answers the question, "
    "answer from it without calling a specialist. topic is one of: {topics}. "
    "tax_year is optional, for example 2026. A REVIEWED fact wins over a specialist answer that differs. "
    "OPEN items are unsettled: say so and refer staff to a supervisor instead of answering, and leave out "
    "anything a specialist says on an OPEN point, even with attribution. If the result "
    "says there is no reviewed figure for a tax year, don't give one."
)


def _load():
    with open(FACTS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    facts = data.get("facts", [])
    topics = data.get("topics") or sorted({f.get("topic", "") for f in facts if f.get("topic")})
    return topics, facts


def _status(f):
    s = (f.get("status") or "draft").strip().lower()
    if s == "reviewed" and not (f.get("reviewed_by") and f.get("last_reviewed")):
        return "draft"  # can't count as reviewed without a name and a date
    return s if s in ("reviewed", "draft", "open") else "draft"


def _overdue(f, today):
    try:
        d = dt.date.fromisoformat(str(f.get("last_reviewed")))
    except ValueError:
        return True
    return (today - d).days > REVIEW_MAX_DAYS


def key_facts(topic: str = "", tax_year: int | None = None, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    topics, facts = _load()
    t = (topic or "").strip().lower()
    if t not in [x.lower() for x in topics]:
        return "Pick a topic: " + "; ".join(topics) + "."

    rows = [f for f in facts if str(f.get("topic", "")).lower() == t]
    year_rows = [f for f in rows if f.get("tax_year") is not None]
    if tax_year is not None:
        rows = [f for f in rows if f.get("tax_year") in (None, tax_year)]

    reviewed = [f for f in rows if _status(f) == "reviewed"]
    open_ = [f for f in rows if _status(f) == "open"]
    draft = [f for f in rows if _status(f) == "draft"]

    head = f"KEY FACTS: {topic.strip()}" + (f" (tax year {tax_year})" if tax_year is not None else "")
    out = [head]

    if tax_year is not None and year_rows:
        this_year = [f for f in year_rows if f.get("tax_year") == tax_year]
        if not this_year:
            out.append(
                f"No figure on file for tax year {tax_year}. Don't give one. Say it isn't published or confirmed yet."
            )
        elif not any(_status(f) == "reviewed" for f in this_year):
            out.append(
                f"The tax year {tax_year} figure isn't signed off yet. If you give the specialist's figure, "
                "say it hasn't been checked against reviewed facts."
            )

    if reviewed:
        out.append("\nREVIEWED (use these over any specialist figure that differs)")
        for f in reviewed:
            yr = f", tax year {f['tax_year']}" if f.get("tax_year") is not None else ""
            flag = " REVIEW OVERDUE, say so." if _overdue(f, today) else ""
            out.append(
                f"- [{f['id']}{yr}] {f['fact']} | Source: {f.get('source', '')} | "
                f"Reviewed by {f['reviewed_by']}, {f['last_reviewed']}.{flag}"
            )
    else:
        out.append("\nNo reviewed facts for this topic yet. Say the answer hasn't been checked against reviewed facts.")

    if open_:
        out.append(
            "\nOPEN (not settled. Don't state an answer, and leave out anything a specialist says on these points.)"
        )
        for f in open_:
            out.append(f"- [{f['id']}] {f['fact']} Next step: {f.get('ask', 'refer staff to a supervisor')}")

    if draft:
        out.append("\nDRAFT (not signed off; don't use as a check)")
        for f in draft:
            out.append(f"- [{f['id']}] {f['fact']}")

    _log("key facts", topic=repr(t), tax_year=tax_year, reviewed=len(reviewed), open=len(open_), draft=len(draft))
    return "\n".join(out)


def register(mcp):
    try:
        topics, _ = _load()
    except Exception as e:  # noqa: BLE001  the tool still registers and reports the problem when called
        _log("key facts file problem", error=str(e))
        topics = ["(facts file unreadable)"]

    @mcp.tool(name="ACES_Key_Facts", description=DESCRIPTION.format(topics="; ".join(topics)))
    def ACES_Key_Facts(topic: str = "", tax_year: int | None = None) -> str:
        return key_facts(topic, tax_year)
