"""Sign off rows in aces_facts.json. Run from the repo folder.

  python sign_facts.py --list
  python sign_facts.py "Aaron Newe" vf_income_limit_2026 office_contact ...
  python sign_facts.py --cosign "Topic Owner" vf_income_limit_2026 ...

Signing sets status to reviewed, reviewed_by to your name and last_reviewed to today for
each id you list. It won't sign open rows (settle them and rewrite the fact first) or ids
it doesn't recognize.

Co-signing is for the person who owns the topic, after checking each row against its
source. It needs a row that's already reviewed and a name other than the reviewer's, and
records a fingerprint of the row so the co-signature lapses if the row changes. Re-signing
a row that changed since it was co-signed clears the old co-signature.

Either way, nothing changes if any id is wrong. Commit the file afterwards."""

import datetime as dt
import json
import sys
from pathlib import Path

from aces_facts import cosign_state, fingerprint

FACTS_FILE = Path(__file__).resolve().parent / "aces_facts.json"


def _list(data) -> None:
    for f in data["facts"]:
        status = f.get("status", "draft")
        line = f"{status:9} {f['id']}"
        if status == "reviewed":
            line += f" ({f.get('reviewed_by')}, {f.get('last_reviewed')})"
            state = cosign_state(f)
            if state == "cosigned":
                line += f"; co-signed by {f.get('cosigned_by')}, {f.get('cosigned')}"
            elif state == "stale":
                line += "; co-signature out of date, the row changed"
            else:
                line += "; needs a co-signature"
        print(line)


def main(argv: list[str], path: Path = FACTS_FILE, today: dt.date | None = None) -> int:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = {f["id"]: f for f in data["facts"]}

    if not argv or argv[0] == "--list":
        _list(data)
        return 0

    cosign = argv[0] == "--cosign"
    args = argv[1:] if cosign else argv
    if len(args) < 2:
        print("Give a name and at least one id.")
        return 2
    name, ids = args[0].strip(), args[1:]
    if not name:
        print("The name can't be blank.")
        return 2

    problems = []
    bad = [i for i in ids if i not in rows]
    if bad:
        problems.append("Unknown ids: " + ", ".join(bad))
    if cosign:
        unsigned = [
            i for i in ids if i in rows and not (rows[i].get("status") == "reviewed" and rows[i].get("reviewed_by"))
        ]
        if unsigned:
            problems.append("Only reviewed rows can be co-signed: " + ", ".join(unsigned))
        own = [i for i in ids if i in rows and str(rows[i].get("reviewed_by", "")).strip().lower() == name.lower()]
        if own:
            problems.append("You reviewed these, so someone else has to co-sign them: " + ", ".join(own))
    else:
        opened = [i for i in ids if i in rows and rows[i].get("status") == "open"]
        if opened:
            problems.append("Open rows can't be signed until they're settled: " + ", ".join(opened))
    if problems:
        for problem in problems:
            print(problem)
        print("Nothing changed.")
        return 1

    day = (today or dt.date.today()).isoformat()
    for i in ids:
        row = rows[i]
        if cosign:
            row.update(cosigned_by=name, cosigned=day, cosign_fingerprint=fingerprint(row))
        else:
            if row.get("cosign_fingerprint") and row["cosign_fingerprint"] != fingerprint(row):
                for key in ("cosigned_by", "cosigned", "cosign_fingerprint"):
                    row.pop(key, None)
            row.update(status="reviewed", reviewed_by=name, last_reviewed=day)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=True)
    what = "Co-signed" if cosign else "Signed"
    print(f"{what} {len(ids)} row(s) as {name}, {day}:", ", ".join(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
