"""Play a case with one worker per supplier thread, so no thread waits on another's reply.

The kit's `play` sends one message at a time: in an RFQ the other threads' 60-second clocks run
out while one supplier takes a minute to answer. Here every thread has its own worker; decisions
share one agent under a lock; the first accept or walk-away ends the case for all.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from arena_kit.client import ArenaError
from arena_kit.play import (
    CLOSE_BEFORE_CAP_S,
    ENDED,
    ROUND_MARGIN_S,
    RemoteTable,
    closing_move,
    fetch_documents,
    latest,
    standing,
)

log = logging.getLogger("pegasus")


def play_case(table, case_id, agent, deadline=None):
    case, threads = table.open(case_id)
    if getattr(table, "done", None):
        return table.done
    for events in threads.values():
        for m in events:
            fetch_documents(table, m)
    if hasattr(agent, "observe"):
        for s_id, events in threads.items():
            for m in events:
                agent.observe(s_id, m)
    lock, done = threading.Lock(), threading.Event()
    box, slowest, inflight = {}, [0.0], set()

    def finish(sid, msg):
        """Close the case; False if the arena refused (e.g. a stale sheet) - then decide again."""
        try:
            box["result"] = table.close(sid, msg)
        except ArenaError as e:
            log.warning("%s: close failed: %s", case_id, e.detail)
            if e.status in (409, 422) and table.client.session(table.session_id).get("status") != "ended":
                return False
            box["result"] = table.result()
        done.set()
        return True

    def worker(sid):
        while not done.is_set():
            t0 = time.monotonic()
            with lock:
                if done.is_set() or latest(threads[sid]).get("status") in ENDED:
                    return
                cap = deadline() if callable(deadline) else deadline
                if cap and time.time() > cap - max(CLOSE_BEFORE_CAP_S, slowest[0] + ROUND_MARGIN_S):
                    decision = agent.closing(case, threads) if hasattr(agent, "closing") else closing_move(case, threads)
                else:
                    decision = agent.decide(case, threads, sid, busy=inflight - {sid})
                if decision is None:
                    return
                waiting = bool(decision.get("wait"))
            if waiting:
                time.sleep(1.0)
                continue
            with lock:
                if done.is_set():
                    return
                dsid, msg = decision["supplier_id"], decision["message"]
                if msg.get("action"):
                    log.info("%s %s %s", case_id, dsid, msg["action"])
                    if finish(dsid, msg):
                        return
                    if hasattr(agent, "refuse") and msg["action"].get("term_sheet_id"):
                        agent.refuse(msg["action"]["term_sheet_id"])
                    # refused (e.g. the sheet went stale): resync that thread from the arena
                    try:
                        fresh = table.client.thread(table.session_id, dsid)["messages"]
                        threads[dsid] = [fetch_documents(table, m) for m in fresh]
                    except ArenaError as e:
                        log.warning("%s: resync failed: %s", case_id, e.detail)
                        return
                    continue
                inflight.add(dsid)
            try:
                reply = table.send(dsid, msg)
            except ArenaError as e:
                if e.status != 409:
                    with lock:
                        inflight.discard(dsid)
                    log.warning("%s %s: %s", case_id, dsid, e.detail)
                    if e.status == 422:  # an invalid message uses no round: log and stop this thread
                        return
                    raise
                reply = table.missed(dsid, e)
            if "result" in reply:
                box["result"] = reply["result"]
                done.set()
                return
            if hasattr(agent, "observe"):
                agent.observe(dsid, reply)  # the reader's LLM call runs outside the decision lock
            with lock:
                threads[dsid] += [{**msg, "from": "buyer"}, fetch_documents(table, reply)]
                inflight.discard(dsid)  # only now may other threads act on this supplier's sheets
                slowest[0] = max(slowest[0], time.monotonic() - t0)
                log.info("%s %s %s %s", case_id, dsid, reply.get("status"), standing(threads[dsid])[1])

    with ThreadPoolExecutor(max_workers=len(threads)) as pool:
        list(pool.map(worker, list(threads)))
    result = box.get("result") or table.result()
    if hasattr(agent, "finish"):
        agent.finish(case, threads, result)
    return result


def play_cases(client, case_ids, new_agent, parallel=5):
    def one(cid):
        try:
            return cid, play_case(RemoteTable(client), cid, new_agent())
        except Exception as e:
            log.exception("%s failed", cid)
            return cid, e

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return dict(pool.map(one, case_ids))


def play_waves(client, new_agent, waves=5, parallel=10, poll=5.0):
    """The Live Final, like the kit's `play_waves`, with the per-thread player."""
    import functools

    started, closed, caps = {}, set(), {}

    def one(case, deadline):
        try:
            return play_case(RemoteTable(client, case), case["case_id"], new_agent(), deadline)
        except Exception as e:
            log.exception("%s failed", case["case_id"])
            return e

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        while len(closed) < waves or not all(f.done() for f in started.values()):
            try:
                cases = client.cases("final")
            except ArenaError as e:
                if e.status != 403:
                    raise
                cases = []
            for case in cases:
                if not case.get("open"):
                    closed.add(case["wave"])
                    continue
                if case.get("seconds_left") is not None:
                    caps[case["wave"]] = time.time() + case["seconds_left"]
                if case["case_id"] not in started and case.get("seconds_left"):
                    log.warning("wave %s: %s", case["wave"], case["case_id"])
                    started[case["case_id"]] = pool.submit(one, case, functools.partial(caps.get, case["wave"]))
            time.sleep(poll)
    return {cid: f.result() for cid, f in started.items()}
