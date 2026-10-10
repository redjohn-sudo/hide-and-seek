"""Sandbox bench (free, outside the practice quota): play D01 and D02 `n` times each at once
and print the mean score per case. Variants via PEGASUS_* environment variables.
uv run python -m pegasus.bench 3"""

import logging
import os
import statistics
import sys

from arena_kit import ArenaClient
from arena_kit.cli import load_env
from arena_kit.data import DataPackage

from .agent import PegasusAgent
from .play import play_cases


def main():
    load_env()
    logging.basicConfig(level=logging.WARNING)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    pkg = DataPackage(os.environ.get("ARENA_DATA", "data/package"))
    ids = ["case:D01"] * n + ["case:D02"] * n
    client = ArenaClient()
    from concurrent.futures import ThreadPoolExecutor
    from .play import play_case
    from arena_kit.play import RemoteTable

    def one(cid):
        try:
            return cid, play_case(RemoteTable(client), cid, PegasusAgent(pkg))
        except Exception as e:
            return cid, e

    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        res = list(pool.map(one, ids))
    by = {}
    for cid, r in res:
        by.setdefault(cid, []).append(0.0 if isinstance(r, Exception) else (r.get("score") or 0.0))
    tag = " ".join(f"{k}={v}" for k, v in os.environ.items() if k.startswith("PEGASUS_"))
    print(f"[{tag or 'default'}] " + "  ".join(
        f"{c}: mean={statistics.mean(v):.3f} {[round(x, 3) for x in v]}" for c, v in sorted(by.items())))


if __name__ == "__main__":
    main()
