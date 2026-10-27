"""uv run python -m hide_and_seek.cli practice [P01 P04 ...] | sandbox D01 | final

Plays with the hide_and_seek agent and prints one line per case (score, C) plus the replay links.
`--playbook file.json`: per case {"target_c": ...} learnt from practice results.
"""

import argparse
import json
import logging
import os
import sys

from arena_kit import ArenaClient
from arena_kit.cli import load_env
from arena_kit.data import DataPackage
from arena_kit.play import hosted

from .agent import HideAndSeekAgent
from .play import play_cases, play_waves


def main(argv=None):
    load_env()
    p = argparse.ArgumentParser(prog="hide_and_seek")
    p.add_argument("set", choices=["practice", "sandbox", "final"])
    p.add_argument("cases", nargs="*", help="e.g. P01 P04 (default: every hosted case)")
    p.add_argument("--parallel", type=int, default=5)
    p.add_argument("--playbook")
    p.add_argument("--push-after-final", action="store_true")
    p.add_argument("-v", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.v else logging.WARNING, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data_dir = os.environ.get("ARENA_DATA", "data/package")
    base = DataPackage(data_dir)  # fail now, before any session opens, if the package is missing
    index = base._ids  # the ID index, built once here: read-only from now on, so the agents can share it

    def package():
        """One package per agent (Nevil, team PR #9): a DataPackage holds a single SQLite connection,
        not safe to share between the parallel cases ("no such table: None"); the ID index is shared."""
        pk = DataPackage(data_dir)
        pk.__dict__["_ids"] = index  # a cached_property lives in the instance's __dict__
        return pk
    playbook = json.load(open(a.playbook)) if a.playbook else {}
    new_agent = lambda: HideAndSeekAgent(package(), playbook, a.push_after_final)  # noqa: E731
    client = ArenaClient()
    if a.set == "final":
        results = play_waves(client, new_agent, parallel=a.parallel)
    else:
        ids = [c if c.startswith("case:") else f"case:{c}" for c in a.cases]
        if not ids:
            ids = [c["case_id"] for c in hosted(client.cases(a.set))]
        results = play_cases(client, ids, new_agent, parallel=a.parallel)
    scores = []
    for cid, r in sorted(results.items()):
        if isinstance(r, Exception):
            print(f"{cid}: ERROR {r}")
            continue
        scores.append(r.get("score") or 0)
        print(f"{cid}: score={r.get('score')} deal={r.get('deal')} c_deal={r.get('c_deal')} "
              f"c_open={r.get('c_open')} c_best={r.get('c_best')} terms={json.dumps(r.get('terms'))}")
    if scores:
        print(f"mean score: {100 * sum(scores) / len(scores):.1f}")
    try:
        print("practice left:", client.me()["practice"]["left"])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
