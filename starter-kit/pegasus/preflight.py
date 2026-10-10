"""Pre-flight before the Live Final: everything the runner needs, checked in one go, no secrets printed.

uv run python -m pegasus.preflight
Exit code 1 if anything is red. Pattern from the team's oblivion project (`entraide/verification.py`:
a list of (ok, message), no secret shown, clock drift against the server's Date header).
"""

import email.utils
import json
import os
import sys
import time
from pathlib import Path

import httpx

from arena_kit.cli import load_env


def check():
    load_env()
    out = []
    key, url = os.environ.get("ARENA_API_KEY", ""), os.environ.get("ARENA_URL", "").rstrip("/")
    out.append((bool(key) and bool(url), f"ARENA_URL / ARENA_API_KEY set ({'yes' if key else 'missing'})"))
    try:
        r = httpx.get(f"{url}/health", timeout=15)
        out.append((r.status_code == 200, f"arena /health {r.status_code}"))
        srv = email.utils.parsedate_to_datetime(r.headers["date"]).timestamp() if "date" in r.headers else None
        drift = abs(time.time() - srv) if srv else None
        out.append((drift is not None and drift < 30, f"clock drift vs arena {drift:.0f}s" if drift is not None else "no Date header"))
    except Exception as e:
        out.append((False, f"arena unreachable: {type(e).__name__}"))
    try:
        r = httpx.get(f"{url}/v1/me", headers={"X-API-Key": key}, timeout=15)
        me = r.json() if r.status_code == 200 else {}
        out.append((r.status_code == 200 and me.get("status") == "active", f"key: {me.get('team_name', '?')} status {me.get('status', r.status_code)}"))
    except Exception as e:
        out.append((False, f"/v1/me failed: {type(e).__name__}"))
    try:
        r = httpx.get(f"{url}/v1/data", timeout=15).json()
        rel = r.get("release-sunday") or {}
        out.append((bool(rel.get("public")), f"sunday release public: {rel.get('public')} (at {rel.get('at')})"))
    except Exception as e:
        out.append((False, f"/v1/data failed: {type(e).__name__}"))
    data = Path(os.environ.get("ARENA_DATA", "data/package"))
    out.append(((data / "facts.json").is_file(), f"data package at {data}"))
    rel_dir = data / "release-sunday"
    out.append((rel_dir.is_dir() and (rel_dir / "facts.json").is_file(), f"sunday release unpacked at {rel_dir} ({'yes' if rel_dir.is_dir() else 'NO - run: uv run arena data'})"))
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    model = os.environ.get("LLM_MODEL", "")
    t0 = time.monotonic()
    try:
        body = {"model": model, "messages": [{"role": "user", "content": "Reply with the single word OK"}], "max_tokens": 50}
        r = httpx.post(f"{base}/chat/completions", json=body, timeout=45,
                       headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"})
        took = time.monotonic() - t0
        ok = r.status_code == 200 and "choices" in r.json()
        out.append((ok and took < 20, f"LLM {model} reachable in {took:.1f}s (status {r.status_code})"))
    except Exception as e:
        out.append((False, f"LLM failed: {type(e).__name__}"))
    out.append((os.environ.get("PEGASUS_READER", "0") == "1", f"PEGASUS_READER={os.environ.get('PEGASUS_READER', '0')} (1 expected for the final)"))
    out.append((os.environ.get("PEGASUS_TEAM_CLAIMS", "1") == "1", f"PEGASUS_TEAM_CLAIMS={os.environ.get('PEGASUS_TEAM_CLAIMS', '1')}"))
    try:
        from . import adapt, agent, economics, guard, memory, play, reader  # noqa: F401
        out.append((True, "pegasus modules import"))
    except Exception as e:
        out.append((False, f"pegasus import failed: {e}"))
    return out


def main():
    results = check()
    for ok, msg in results:
        print(("OK  " if ok else "RED ") + msg)
    bad = [m for ok, m in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks green")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
