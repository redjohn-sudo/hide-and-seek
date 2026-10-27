"""Play a case with one worker per supplier thread, so no thread waits on another's reply.

The kit's `play` sends one message at a time: in an RFQ the other threads' 60-second clocks run
out while one supplier takes a minute to answer. Here every thread has its own worker; decisions
share one agent under a lock; the first accept or walk-away ends the case for all.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

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

log = logging.getLogger("hide_and_seek")
# total wait for the opening readings (HIDE_AND_SEEK_READER=1) before the workers start
OPEN_READ_BUDGET_S = float(os.environ.get("HIDE_AND_SEEK_OPEN_READ_BUDGET_S", "12"))


def closing(agent, case, threads):
    """The agent's closing move (its cost, relationship charge included), else the kit's: also when
    the agent's own closing raises, so that a case is always closed."""
    if hasattr(agent, "closing"):
        try:
            return agent.closing(case, threads)
        except Exception:
            log.exception("%s: agent closing failed, kit closing move", case.get("case_id"))
    return closing_move(case, threads)


def observe_openings(agent, threads, budget=None):
    """Read each thread's latest supplier message (the only one `decide` looks up) in parallel, and
    wait at most `budget` seconds in all: the 60-second clock runs from the supplier's opening, and
    in series five readings of up to 20 s each would spend it before our first message. A reading
    still running after the budget finishes in the background; `decide` meanwhile reads the message
    with keywords and takes the LLM's reading once it is there."""
    budget = OPEN_READ_BUDGET_S if budget is None else budget
    todo = []
    for s_id, events in threads.items():
        m = next((x for x in reversed(events) if x.get("from") != "buyer"), None)
        if m and m.get("body"):
            todo.append((s_id, m))
    if not todo:
        return
    pool = ThreadPoolExecutor(max_workers=len(todo))
    futures = [pool.submit(agent.observe, s_id, m) for s_id, m in todo]
    _, pending = wait(futures, timeout=budget)
    if pending:
        log.warning("opening reads: %d of %d still running after %.0f s - deciding with keywords meanwhile",
                    len(pending), len(todo), budget)
    pool.shutdown(wait=False)


def play_case(table, case_id, agent, deadline=None):
    case, threads = table.open(case_id)
    if getattr(table, "done", None):
        return table.done
    for events in threads.values():
        for m in events:
            fetch_documents(table, m)
    if hasattr(agent, "observe"):
        observe_openings(agent, threads)
    lock, done = threading.Lock(), threading.Event()
    box, slowest, inflight, alive = {}, [0.0], set(), [len(threads)]

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

    def close_open(why):
        """No worker is left but the case is still open (e.g. a 422 on the only thread, a failed
        resync): close it now with the closing move - an open case scores 0. A refused sheet is
        dropped and the move recomputed (at most three tries)."""
        for _ in range(3):
            with lock:
                if done.is_set():
                    return
                try:
                    move = closing(agent, case, threads)
                except Exception:  # e.g. the kit's closing move with no live thread left
                    log.exception("%s: no closing move", case_id)
                    return
                if not move:
                    return
                log.warning("%s: %s - no worker left, closing move %s", case_id, why, move["message"].get("action"))
                if finish(move["supplier_id"], move["message"]):
                    return
                sheet = (move["message"].get("action") or {}).get("term_sheet_id")
                if hasattr(agent, "refuse") and sheet:
                    agent.refuse(sheet)

    def worker(sid):
        why = "worker ended"
        try:
            why = run(sid) or why
        except BaseException:
            why = "worker failed"
            raise
        finally:
            with lock:
                alive[0] -= 1
                last = alive[0] == 0
            if last and not done.is_set():
                try:
                    close_open(why)
                except Exception:
                    log.exception("%s: closing move failed", case_id)

    def run(sid):
        """One supplier thread until the case ends; returns why it stopped early, if it did."""
        while not done.is_set():
            t0 = time.monotonic()
            with lock:
                if done.is_set() or latest(threads[sid]).get("status") in ENDED:
                    return
                cap = deadline() if callable(deadline) else deadline
                if cap and time.time() > cap - max(CLOSE_BEFORE_CAP_S, slowest[0] + ROUND_MARGIN_S):
                    log.info("%s: the wave's cap is near - closing", case_id)
                    decision = closing(agent, case, threads)
                else:
                    try:
                        decision = agent.decide(case, threads, sid, busy=inflight - {sid})
                    except Exception:
                        # a bug in the agent must not leave the session open (an open case scores 0)
                        log.exception("%s %s: decide failed, closing move", case_id, sid)
                        decision = closing(agent, case, threads)
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
                        return "resync failed"
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
                        return "422 on send"  # the last worker out closes the case (see `worker`)
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

    try:
        with ThreadPoolExecutor(max_workers=len(threads)) as pool:
            list(pool.map(worker, list(threads)))
    except Exception:
        # a worker died (e.g. a send failed): the last worker out has already tried the closing move
        if done.is_set():
            log.exception("%s: a worker failed; the case is closed", case_id)
        elif table.client.session(table.session_id).get("status") == "ended":
            log.exception("%s: a worker failed; the case has ended", case_id)
        else:
            log.exception("%s: a worker failed, closing move", case_id)
            move = closing(agent, case, threads)
            if not move or not finish(move["supplier_id"], move["message"]):
                raise
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
