"""Offline rehearsal of the Live Final path: play_waves -> play_case -> decide/closing/finish against
a fake arena (one wave, two cases, a supplier that concedes then holds, a cap that forces the
closing move on one case). No network, no quota. `uv run python -m pegasus.dryrun_final`"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("PEGASUS_READER", "0")
os.environ["PEGASUS_MEMORY"] = tempfile.mktemp(suffix=".json")

from arena_kit.cli import load_env  # noqa: E402

load_env()
from arena_kit.data import DataPackage  # noqa: E402

import pegasus.memory as memory  # noqa: E402

memory.FILE = Path(os.environ["PEGASUS_MEMORY"])
from pegasus.agent import PegasusAgent  # noqa: E402
from pegasus.play import play_waves  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
# the practice case files: PEGASUS_CASES, else the copy in this repository (analysis/cases-practice.json)
CASES_FILE = Path(os.environ.get("PEGASUS_CASES") or REPO / "analysis" / "cases-practice.json").expanduser()
CASES = {c["case_id"]: c for c in json.loads(CASES_FILE.read_text(encoding="utf-8"))}
# the data package: ARENA_DATA, else data/package (the kit's layout), else this repository's data/
DATA = next((d for d in (os.environ.get("ARENA_DATA"), "data/package", str(REPO / "data"))
             if d and (Path(d) / "facts.json").is_file()), os.environ.get("ARENA_DATA") or "data/package")


class FakeArena:
    """Just enough of the API for the final path. Supplier: -1 % per round, final_offer after 4
    replies on P04; P07's supplier never closes, so the wave cap must close it."""

    def __init__(self, case_ids, cap_s):
        self.cases_ = [CASES[c] for c in case_ids]
        self.start, self.cap_s = time.time(), cap_s
        self.sessions, self.lock, self.n = {}, threading.Lock(), 0

    # -- the case list, wave by wave
    def cases(self, set_="practice"):
        if set_ != "final":
            return []
        left = max(0.0, self.cap_s - (time.time() - self.start))
        done = self.sessions and all(s["status"] == "ended" for s in self.sessions.values())
        return [{**c, "set": "hidden", "wave": 1, "theme": "Rehearsal", "open": left > 0 and not done,
                 "seconds_left": left} for c in self.cases_]

    def open_session(self, case_id):
        with self.lock:
            self.n += 1
            case = CASES[case_id]
            sid = case["suppliers"][0]
            up = 1.01 if case_id == "case:P07" else 1.06  # the slow one can beat our alternative within two replies
            price = round((case["buyer"].get("current_unit_price") or case["buyer"]["fallback_unit_price"]) * up, 2)
            terms = {f: (case["term_options"].get(f) or [None])[0] for f in case["negotiable"] if f != "unit_price"}
            terms["unit_price"] = price
            opening = {"from": f"sales@{sid[4:]}", "status": "open", "body": "Please find our quote attached. LCI-C rose 2.7 % in twelve months.",
                       "term_sheets": [{"term_sheet_id": f"offer:{sid[4:]}-0", "terms": terms}], "attachments": [],
                       "claim_results": [], "missing_fields": []}
            s = {"session_id": f"fake{self.n}", "case_id": case_id, "status": "open", "threads": {sid: [opening]},
                 "n": 0, "price": price, "opening": price, "result": None}
            self.sessions[s["session_id"]] = s
            return {"session_id": s["session_id"], "case": case, "threads": {sid: [opening]}}

    def send_message(self, session_id, supplier_id, message):
        with self.lock:
            s = self.sessions[session_id]
            case = CASES[s["case_id"]]
            if s["status"] == "ended":
                from arena_kit.client import ArenaError
                raise ArenaError(409, "the session has ended")
            s["threads"][supplier_id].append({**message, "from": "buyer"})
            standing = s["threads"][supplier_id][-2]["term_sheets"][0] if len(s["threads"][supplier_id]) >= 2 else None
            if message.get("action"):
                terms = standing["terms"] if standing else {}
                s["status"] = "ended"
                s["result"] = {"ended": True, "deal": message["action"]["type"] == "accept", "supplier_id": supplier_id,
                               "terms": terms, "score": 0.5, "c_open": 1.0, "c_deal": 0.9, "c_best": 0.8}
                return {"result": s["result"]}
            s["n"] += 1
            s["price"] = round(s["price"] * 0.99, 2)
            ours = (message.get("term_sheets") or [{}])[0].get("terms") or {}
            # like the real bots: it answers our first package at its own price
            terms = {**(standing["terms"] if standing else {}), **{k: v for k, v in ours.items() if k != "unit_price"}, "unit_price": s["price"]}
            status = "final_offer" if (s["case_id"] == "case:P04" and s["n"] >= 4) else "open"
            slow = s["case_id"] == "case:P07"  # a slow supplier: the wave cap must close this one
            reply = {"from": f"sales@{supplier_id[4:]}", "status": status, "attachments": [], "claim_results": [], "missing_fields": [],
                     "body": f"We can move to {s['price']} EUR. Which volume can you commit to?",
                     "term_sheets": [{"term_sheet_id": f"offer:{supplier_id[4:]}-{s['n']}", "terms": terms}]}
            s["threads"][supplier_id].append(reply)
        if slow:
            time.sleep(2.5)
        return reply

    def thread(self, session_id, supplier_id):
        s = self.sessions[session_id]
        last = next(m for m in reversed(s["threads"][supplier_id]) if m.get("from") != "buyer")
        return {"supplier_id": supplier_id, "offer_id": last["term_sheets"][0]["term_sheet_id"], "status": last["status"],
                "closed": None, "messages": s["threads"][supplier_id], "timeouts": []}

    def session(self, session_id):
        s = self.sessions[session_id]
        return {"session_id": session_id, "case_id": s["case_id"], "status": s["status"], "end_reason": "accepted" if s["result"] else None,
                "threads": {sid: {"status": "open", "seconds_left": 60} for sid in s["threads"]}, "result": s["result"]}

    def attachment(self, session_id, attachment_id):
        return b""


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    pkg = DataPackage(DATA)
    arena = FakeArena(["case:P04", "case:P07"], cap_s=96)  # closing fires ~6 s in (cap - 90 s)
    t0 = time.time()
    results = play_waves(arena, lambda: PegasusAgent(pkg), waves=1, parallel=10, poll=1.0)
    took = time.time() - t0
    ok = True
    for cid, r in results.items():
        bad = isinstance(r, Exception) or not r.get("deal")
        ok &= not bad
        print(("RED " if bad else "OK  ") + f"{cid}: {r if isinstance(r, Exception) else {k: r.get(k) for k in ('deal', 'supplier_id')}}")
    mem = json.loads(memory.FILE.read_text()) if memory.FILE.exists() else {}
    ok &= bool(mem)
    print(("OK  " if mem else "RED ") + f"supplier memory written for {sorted(mem)}")
    print(f"{'OK ' if took < 90 else 'RED'} wave finished in {took:.0f}s (cap 96 s)")
    return 0 if ok and took < 90 else 1


if __name__ == "__main__":
    sys.exit(main())
