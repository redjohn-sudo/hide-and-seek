"""Offline tests for the hide_and_seek agent: economics, reader, guard paths, the per-thread player.

No arena, no LLM: the practice case files of this repository (analysis/cases-practice.json) and its
data package (data/), fake tables for the player."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CASES_FILE = REPO / "analysis" / "cases-practice.json"
DATA = REPO / "data"

pytestmark = pytest.mark.skipif(not CASES_FILE.is_file(), reason="no practice case files")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Supplier memory and the reader archive in a temporary folder; reader off unless a test turns it on."""
    from hide_and_seek import memory, reader, reader_cache

    monkeypatch.setattr(memory, "FILE", tmp_path / "memory.json")
    monkeypatch.setattr(memory, "_mem", {})
    monkeypatch.setattr(reader_cache, "FILE", tmp_path / "reader.json")
    monkeypatch.setattr(reader_cache, "_archive", {})
    monkeypatch.setattr(reader, "ENABLED", False)
    monkeypatch.setattr(reader, "_llm", _no_llm)


def _no_llm(lines):
    raise AssertionError("no LLM call in the tests")


@pytest.fixture(scope="module")
def cases():
    return {c["case_id"]: c for c in json.loads(CASES_FILE.read_text(encoding="utf-8"))}


@pytest.fixture(scope="module")
def pkg():
    if not (DATA / "facts.json").is_file():
        pytest.skip("no data package")
    from arena_kit.data import DataPackage

    return DataPackage(DATA)


def supplier(sid, terms, status="open", body="Please find our offer attached.", n=0, alts=()):
    sheets = [{"term_sheet_id": f"offer:{sid[4:]}-{n}", "terms": terms}]
    sheets += [{"term_sheet_id": f"offer:{sid[4:]}-{n}-a{i}", "terms": t} for i, t in enumerate(alts)]
    return {"from": f"sales@{sid[4:]}", "status": status, "body": body, "term_sheets": sheets, "attachments": []}


def buyer(terms_list, body="..."):
    return {"from": "buyer", "body": body, "term_sheets": [{"terms": t} for t in terms_list]}


# -- economics ---------------------------------------------------------------------------------
def test_implied_rate_from_a_payment_pair():
    from hide_and_seek.economics import implied_rate

    r = implied_rate([{"unit_price": 373.88, "payment_days": 90}, {"unit_price": 368.68, "payment_days": 14}])
    assert r == pytest.approx(0.066, abs=0.002)


def test_implied_rate_tolerates_missing_or_null_fields():
    from hide_and_seek.economics import implied_rate

    # regression: a sheet without payment_days raised KeyError (economics.py:138)
    assert implied_rate([{"unit_price": 373.88, "payment_days": 90}, {"unit_price": 368.68}]) is None
    assert implied_rate([{"unit_price": 373.88, "payment_days": None}, {"unit_price": 368.68, "payment_days": 14}]) is None
    assert implied_rate([{"unit_price": None, "payment_days": 90}, {"unit_price": 368.68, "payment_days": 14}]) is None
    assert implied_rate([]) is None and implied_rate([{"unit_price": 1.0}]) is None
    # a valid pair beside a broken sheet still counts
    r = implied_rate([{"unit_price": 373.88, "payment_days": 90}, {"unit_price": 368.68, "payment_days": 14},
                      {"unit_price": 360.0}])
    assert r == pytest.approx(0.066, abs=0.002)


def test_implied_rate_ignores_implausible_rates():
    from hide_and_seek.economics import implied_rate

    assert implied_rate([{"unit_price": 100.0, "payment_days": 90}, {"unit_price": 50.0, "payment_days": 14}]) is None


# -- reader ------------------------------------------------------------------------------------
@pytest.mark.parametrize("sig,line,quote,expect", [
    ({"type": "lead_time_firm", "value": 54}, "Our lead time of 54 days is firm.", "lead time of 54 days is firm", (True, 54.0)),
    ({"type": "lead_time_firm", "value": 54}, "Our lead time is firm at 54.5 days.", "lead time is firm", (False, None)),
    ({"type": "lead_time_firm", "value": 54}, "We charge 54 EUR for a shorter lead time.", "shorter lead time", (False, None)),
    ({"type": "cost_floor", "value": 28.0}, "28.00 EUR per unit is our absolute limit.", "absolute limit", (True, 28.0)),
    ({"type": "cost_floor", "value": None}, "This is not our absolute limit.", "not our absolute limit", (False, None)),
    ({"type": "requires_index_clause", "value": None}, "An index clause is not required.", "is not required", (False, None)),
    ({"type": "chargeback_too_high", "value": 300}, "A chargeback of 300 EUR is too high, the price stays 5.00 EUR.",
     "chargeback of 300 EUR is too high", (True, 300.0)),
    ({"type": "chargeback_too_high", "value": "abc"}, "chargeback too high", "chargeback too high", (False, None)),
    ({"type": None}, "x", "x", (False, None)),
    ({"type": "free_capacity", "value": None}, "We have free capacity.", "free capacity", (True, None)),
])
def test_reader_validate(sig, line, quote, expect):
    from hide_and_seek.reader import validate

    assert validate(sig, line, quote) == expect


def test_reader_keywords_only_never_calls_the_llm(monkeypatch):
    from hide_and_seek import reader

    monkeypatch.setattr(reader, "ENABLED", True)
    found = reader.read("We cannot go any lower. Faster payment would help our cash flow.", llm=False)
    assert {"cost_floor", "wants_faster_payment"} <= found


# -- the agent ---------------------------------------------------------------------------------
P04 = "sup:70620"
P04_OPEN = {"unit_price": 4.90, "payment_days": 30, "contract_months": 24}


def p04_thread(body, price=4.90, status="open"):
    return [supplier(P04, {**P04_OPEN, "unit_price": 4.95}),
            buyer([{**P04_OPEN, "unit_price": 4.10}]),
            supplier(P04, {**P04_OPEN, "unit_price": price}, status=status, body=body, n=1)]


@pytest.mark.parametrize("written", ["4.80 EUR", "4,80 EUR", "EUR 4.80", "4.80 €", "€ 4,80"])
def test_take_its_word_reads_every_price_format(cases, pkg, written):
    from hide_and_seek.agent import HideAndSeekAgent

    case = cases["case:P04"]
    threads = {P04: p04_thread(f"We hold our terms. I can offer you {written} per unit on these terms.")}
    out = HideAndSeekAgent(pkg).decide(case, threads, P04)
    sheets = out["message"]["term_sheets"]
    assert len(sheets) == 1 and sheets[0]["terms"]["unit_price"] == 4.80
    assert sheets[0]["terms"]["payment_days"] == 30 and sheets[0]["terms"]["contract_months"] == 24
    assert "4.80 EUR per unit" in out["message"]["body"]


@pytest.mark.parametrize("body", [
    "Your price of 4.50 EUR is not possible.",           # our price, not its word
    "With 14 days payment I can offer 4.80 EUR.",        # another package (14 days, not its 30)
    "I can offer 3.00 EUR for a far larger volume.",     # out of the plausible band
])
def test_take_its_word_ignores_other_prices(cases, pkg, body):
    from hide_and_seek.agent import HideAndSeekAgent

    agent = HideAndSeekAgent(pkg)
    case = cases["case:P04"]
    threads = {P04: p04_thread(body)}
    st = agent._prepare(case) or agent._thread(case, P04, threads[P04])
    assert agent._take_its_word(case, threads, P04, st, ()) is None


def test_seen_grows_once_per_supplier_message(cases, pkg):
    """Regression (agent.py:427): every decision appended C_s - the 1-second wait loop and the resync
    after a refused close read as "it stopped moving" (false stall close, false nudge)."""
    from hide_and_seek.agent import HideAndSeekAgent

    agent = HideAndSeekAgent(pkg)
    case = cases["case:P04"]
    threads = {P04: [supplier(P04, P04_OPEN)]}
    for _ in range(4):  # the same reply decided again and again
        agent.decide(case, threads, P04)
    st = agent.state[P04]
    assert len(st["seen"]) == 1
    threads[P04] += [buyer([{**P04_OPEN, "unit_price": 4.0}]), supplier(P04, {**P04_OPEN, "unit_price": 4.80}, n=1)]
    agent.decide(case, threads, P04)
    agent.decide(case, threads, P04)
    assert len(st["seen"]) == 2 and st["seen"][1] < st["seen"][0]


def test_no_false_stall_close_on_repeated_decisions(cases, pkg):
    """Three re-decisions on the same moving reply must not close the case as a stall."""
    from hide_and_seek.agent import HideAndSeekAgent

    agent = HideAndSeekAgent(pkg)
    case = cases["case:P04"]
    events = [supplier(P04, {**P04_OPEN, "unit_price": 4.95})]
    for i, p in enumerate((4.90, 4.85, 4.80), 1):
        events += [buyer([{**P04_OPEN, "unit_price": 4.0 + 0.05 * i}]), supplier(P04, {**P04_OPEN, "unit_price": p}, n=i)]
    threads = {P04: events}
    agent._prepare(case)
    st = agent._thread(case, P04, events)
    st["offers"] = 3
    st["last_target"] = None
    outs = [agent.decide(case, threads, P04)["message"] for _ in range(3)]
    assert len(st["seen"]) == 1
    assert not any(m.get("action") for m in outs[1:])  # its price fell every round: no "it stopped moving" close


def test_decide_never_calls_the_llm_under_the_lock(cases, pkg, monkeypatch):
    """Regression (agent.py:440): a reply not read before the lock was read by the LLM under it; now
    keywords first, the LLM's reading taken once `observe` has it."""
    from hide_and_seek import reader
    from hide_and_seek.agent import HideAndSeekAgent

    monkeypatch.setattr(reader, "ENABLED", True)
    calls = []
    monkeypatch.setattr(reader, "_llm", lambda lines: calls.append(lines) or {"signals": []})
    agent = HideAndSeekAgent(pkg)
    case = cases["case:P04"]
    body = "Please find our offer attached.\nWe have free capacity this year."
    threads = {P04: [supplier(P04, P04_OPEN, body=body)]}
    agent.decide(case, threads, P04)
    assert calls == []
    st = agent.state[P04]
    assert st["read_partial"] and "free_capacity" in st["last_signals"]
    # the opening reading arrives (from observe): it is taken at the next decision
    sig = reader.Signals({"free_capacity", "requires_index_clause"})
    agent._observed[(P04, body[:200])] = sig
    agent.decide(case, threads, P04)
    assert "requires_index_clause" in st["signals_read"] and not st["read_partial"]


def test_floored_raises_prices_and_drops_dear_packages(cases, pkg):
    from hide_and_seek import economics as eco
    from hide_and_seek.agent import HideAndSeekAgent

    case = cases["case:P02"]
    sid = case["suppliers"][0]
    opening = {"unit_price": 373.88, "payment_days": 90, "index_clause": None}
    agent = HideAndSeekAgent(pkg)
    agent._prepare(case)
    st = agent._thread(case, sid, [supplier(sid, opening)])
    assert st["std"].get("_floor"), "P02 is a strategic case with a should-cost"
    rel = eco.with_relationship(st["std"])
    st["c_s_rel"] = eco.standing_cost(case, opening, rel)
    low = {"terms": eco.sheet(case, {"payment_days": 90, "index_clause": None}, 200.0)}
    msg = agent._floored(case, st, {"body": "x", "term_sheets": [low]})
    assert msg["term_sheets"] and msg["term_sheets"][0]["terms"]["unit_price"] > 200.0
    # its standing offer is far cheaper for us than any package we could send: every package goes
    st["c_s_rel"] = 1.0  # cheaper than the bottom of every package's U
    msg = agent._floored(case, st, {"body": "x", "term_sheets": [{"terms": eco.sheet(case, {"payment_days": 90,
                                                                                           "index_clause": None}, 360.0)}]})
    assert msg["term_sheets"] == []


def test_sync_body_without_sheets_drops_attached_wording(cases):
    from hide_and_seek.agent import sync_body

    case = cases["case:P04"]
    body = ("Dear Sir or Madam,\n\nthank you.\nEach of the attached packages works for us; please choose the one that "
            "suits you best.\nWe commit to a binding volume of 20,000 pcs per year over 24 months.\n"
            "We value our cooperation.\n\nBest regards\nHollerau Pumpenwerke GmbH, Purchasing")
    out = sync_body(case, {"body": body, "term_sheets": []}, [{**P04_OPEN, "unit_price": 4.1}])
    assert "attached" not in out["body"]
    assert "Could you confirm your best price" in out["body"]
    assert out["body"].index("Could you") < out["body"].index("We value our cooperation.")


def test_sync_body_follows_the_sheets_as_sent(cases):
    from hide_and_seek.agent import sync_body

    case = cases["case:P04"]
    drafted = [{**P04_OPEN, "unit_price": 4.10, "contract_months": 36}, {**P04_OPEN, "unit_price": 4.10}]
    final = [{"terms": {**P04_OPEN, "unit_price": 4.12}}]  # floored, re-sorted, one left
    body = ("If you can do EUR 4.10 per unit, we commit.\nEach of the attached packages works for us; please choose "
            "the one that suits you best.\nWe commit to a binding volume of 20,000 pcs per year over 36 months.")
    out = sync_body(case, {"body": body, "term_sheets": final}, drafted)["body"]
    assert "EUR 4.12 per unit" in out and "4.10" not in out
    assert "The attached package works for us." in out
    assert "over 24 months." in out


def test_every_message_leaves_through_the_guard(cases, pkg, monkeypatch):
    """Messages written outside `_send` (take its word, mirror, clause probe, questions after a final,
    the nudge line) now pass `guard.check` at the exit."""
    from hide_and_seek.agent import HideAndSeekAgent

    agent = HideAndSeekAgent(pkg)
    case = cases["case:P04"]
    threads = {P04: [supplier(P04, P04_OPEN)]}
    body = "Thank you.\nThis is urgent for us.\nAs shown in news:does-not-exist prices fell.\nPlease confirm."
    monkeypatch.setattr(agent, "_decide", lambda *a, **k: {"supplier_id": P04, "message": {
        "subject": "Re: x", "body": body, "_exact": True, "refs": ["news:does-not-exist"],
        "term_sheets": [{"terms": {**P04_OPEN, "unit_price": 4.5}}]}})
    agent._prepare(case)
    agent._thread(case, P04, threads[P04])
    msg = agent.decide(case, threads, P04)["message"]
    assert "urgent" not in msg["body"] and "news:" not in msg["body"]
    assert msg.get("refs") == [] and "_exact" not in msg and "_drafted" not in msg


def test_internal_keys_never_leave(cases, pkg):
    from hide_and_seek.agent import HideAndSeekAgent

    out = HideAndSeekAgent(pkg).decide(cases["case:P04"], {P04: [supplier(P04, P04_OPEN)]}, P04)
    assert not [k for k in out["message"] if k.startswith("_")]


# -- the player --------------------------------------------------------------------------------
class FakeClient:
    def __init__(self, table):
        self.table = table

    def session(self, session_id):
        return {"status": "ended" if self.table.closed else "open"}

    def thread(self, session_id, sid):
        return {"messages": self.table.threads[sid]}


class FakeTable:
    """One case, scripted replies; `fail` maps a send number to an ArenaError status."""

    def __init__(self, case, threads, fail=None):
        self.case_, self.threads = case, threads
        self.session_id, self.done, self.closed, self.sent, self.fail = "s1", None, None, 0, fail or {}
        self.client = FakeClient(self)

    def open(self, case_id):
        return self.case_, {s: list(ev) for s, ev in self.threads.items()}

    def attachment(self, attachment_id):
        return b""

    def send(self, sid, msg):
        from arena_kit.client import ArenaError

        self.sent += 1
        if self.sent in self.fail:
            raise ArenaError(self.fail[self.sent], "scripted failure")
        last = self.threads[sid][-1]
        terms = {**last["term_sheets"][0]["terms"], "unit_price": round(last["term_sheets"][0]["terms"]["unit_price"] - 0.01, 2)}
        reply = supplier(sid, terms, n=self.sent)
        self.threads[sid] = self.threads[sid] + [{**msg, "from": "buyer"}, reply]
        return reply

    def close(self, sid, msg):
        self.closed = (sid, msg["action"])
        return {"deal": msg["action"]["type"] == "accept", "supplier_id": sid}

    def result(self):
        return {"deal": False, "end_reason": "open" if not self.closed else "closed"}


@pytest.mark.parametrize("status", [422, 500])
def test_single_thread_send_failure_closes_the_case(cases, pkg, status):
    """Regression (play.py:116): a 422 at sending made the only worker return without closing - the
    session stayed open (no deal). Any abnormal end of the last worker now runs the closing move."""
    from hide_and_seek.agent import HideAndSeekAgent
    from hide_and_seek.play import play_case

    case = cases["case:P04"]
    table = FakeTable(case, {P04: [supplier(P04, {**P04_OPEN, "unit_price": 4.60})]}, fail={1: status})
    result = play_case(table, case["case_id"], HideAndSeekAgent(pkg))
    assert table.closed is not None
    assert table.closed[1]["type"] in ("accept", "walk_away")
    assert result.get("supplier_id") == P04


def test_rfq_send_failure_on_one_thread_lets_the_others_finish(cases, pkg):
    from hide_and_seek.agent import HideAndSeekAgent
    from hide_and_seek.play import play_case

    case = dict(cases["case:P04"], suppliers=[P04, "sup:99999"])
    other = "sup:99999"
    table = FakeTable(case, {P04: [supplier(P04, {**P04_OPEN, "unit_price": 4.60})],
                             other: [supplier(other, {**P04_OPEN, "unit_price": 4.70})]}, fail={1: 422})
    play_case(table, case["case_id"], HideAndSeekAgent(pkg))
    assert table.closed is not None  # the case ends closed, whichever thread got the 422


def test_opening_reads_run_in_parallel_within_a_budget():
    from hide_and_seek.play import observe_openings

    seen, lock = [], threading.Lock()

    class Slow:
        def observe(self, sid, m):
            with lock:
                seen.append((sid, m["body"]))
            time.sleep(2.0)

    threads = {f"sup:{i}": [supplier(f"sup:{i}", P04_OPEN, body="old", n=0), buyer([P04_OPEN]),
                            supplier(f"sup:{i}", P04_OPEN, body="latest", n=1)] for i in range(5)}
    t0 = time.monotonic()
    observe_openings(Slow(), threads, budget=0.3)
    took = time.monotonic() - t0
    assert took < 1.5  # five 2-second readings: neither in series (10 s) nor waited out
    time.sleep(0.1)
    assert sorted(b for _, b in seen) == ["latest"] * 5  # only the message `decide` looks up


def test_dryrun_final_finds_its_cases_without_home_paths():
    src = (REPO / "starter-kit" / "hide_and_seek" / "dryrun_final.py").read_text(encoding="utf-8")
    assert "~/tacto" not in src and "HIDE_AND_SEEK_CASES" in src


if os.environ.get("HIDE_AND_SEEK_SLOW_TESTS") == "1":  # pragma: no cover - the full offline rehearsal (about 10 s)
    def test_dryrun_final_green():
        from hide_and_seek import dryrun_final

        assert dryrun_final.main() == 0
