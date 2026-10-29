"""Sign off rows in aces_facts.json. Run from the repo folder.

  python sign_facts.py --list
  python sign_facts.py "Aaron Newe" vf_income_limit_2026 office_contact ...

Sets status to reviewed, reviewed_by to your name and last_reviewed to today for each
id you list. Won't sign open rows (settle them and rewrite the fact first) or ids it
doesn't recognize. Changes nothing if any id is wrong."""

import datetime as dt
import json
import sys

P = "aces_facts.json"
d = json.load(open(P, encoding="utf-8"))
rows = {f["id"]: f for f in d["facts"]}

if len(sys.argv) < 2 or sys.argv[1] == "--list":
    for f in d["facts"]:
        who = f" ({f.get('reviewed_by')}, {f.get('last_reviewed')})" if f.get("status") == "reviewed" else ""
        print(f"{f.get('status', 'draft'):9} {f['id']}{who}")
    sys.exit(0)

name, ids = sys.argv[1], sys.argv[2:]
if not ids:
    sys.exit("List at least one id after your name.")
bad = [i for i in ids if i not in rows]
opened = [i for i in ids if i in rows and rows[i].get("status") == "open"]
if bad or opened:
    if bad:
        print("Unknown ids:", ", ".join(bad))
    if opened:
        print("Open rows can't be signed until they're settled:", ", ".join(opened))
    sys.exit("Nothing changed.")

today = dt.date.today().isoformat()
for i in ids:
    rows[i].update(status="reviewed", reviewed_by=name, last_reviewed=today)
json.dump(d, open(P, "w", encoding="utf-8"), indent=2, ensure_ascii=True)
print(f"Signed {len(ids)} row(s) as {name}, {today}:", ", ".join(ids))
