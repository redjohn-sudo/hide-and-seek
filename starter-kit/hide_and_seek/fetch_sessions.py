"""Download case files and, for every ended session of the team, state+trace and back office."""
import json, pathlib, sys
from arena_kit.cli import load_env
load_env()
from arena_kit import ArenaClient
out = pathlib.Path(sys.argv[1]); (out / "sessions").mkdir(parents=True, exist_ok=True)
c = ArenaClient()
for s in ("practice", "sandbox"):
    (out / f"cases-{s}.json").write_text(json.dumps(c.cases(s), indent=1, ensure_ascii=False))
rows = []
for s in ("practice", "sandbox"):
    for row in c.sessions(s):
        rows.append(row)
        p = out / "sessions" / f"{row['case_id'].split(':')[1]}-{row['session_id']}.json"
        if p.exists() or row["status"] != "ended":
            continue
        d = {"summary": row, "session": c.session(row["session_id"])}
        try:
            d["backoffice"] = c.backoffice(row["session_id"])
        except Exception as e:
            d["backoffice_error"] = str(e)
        p.write_text(json.dumps(d, indent=1, ensure_ascii=False))
(out / "sessions-index.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
print(len(rows), "sessions")
for r in sorted(rows, key=lambda r: r["case_id"]):
    print(r["case_id"], r["session_id"], r["status"], r.get("end_reason"), r.get("score"))
