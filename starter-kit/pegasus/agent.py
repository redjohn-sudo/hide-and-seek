"""The pegasus buyer: code decides every number, a template writes the e-mail.

Per thread it
- prices structures (combinations of the non-price fields) at the price that leaves the supplier
  indifferent, and asks for the ones that are cheap for the supplier and valuable for us
  (full coverage of the need, the cheapest freight, ...);
- sends up to three term sheets that cost *us* the same C, so whichever the supplier picks tells
  us what it values without costing us anything;
- concedes in C, not in price, on a decreasing schedule towards the supplier's standing offer;
- cites only arguments the arena verifies and that argue for us, each ID once;
- accepts an `agreed` offer, a `final_offer`, a stalled supplier or the last round's best offer
  when it beats the alternative and keeps the must-haves - else walks away.
`playbook` (optional): per case, a known target C (e.g. C_best learnt from a practice replay).
"""

import logging
import os
import re

from arena_kit.docs import quote_terms, standard_terms
from arena_kit.play import ENDED, latest, live_threads, moves_left, standing

from . import economics as eco
from . import guard
from .claims import Claims
from .claims_intel import Claims as IntelClaims
from .intel_bridge import Intelligence

log = logging.getLogger("pegasus")

# Tunables (environment, for the sandbox bench): structure ranking and opening anchor.
RANK_INDIFFERENT = os.environ.get("PEGASUS_RANK", "fixed") == "indifferent"
ANCHOR_DROP = float(os.environ.get("PEGASUS_ANCHOR", "0.80"))      # share of the supplier's price
PI_ANCHOR = float(os.environ.get("PEGASUS_PI_ANCHOR", "0.94"))     # price increase: share of current
MIN_STEP = float(os.environ.get("PEGASUS_MIN_STEP", "0.15"))  # share of the gap to their offer
# Share of the remaining gap (anchor -> supplier's standing C) we have conceded after k offers.
SCHEDULE = [float(x) for x in os.environ.get(
    "PEGASUS_SCHEDULE", "0,0.22,0.38,0.52,0.63,0.72,0.80,0.86,0.90,0.93").split(",")]
CASH_RE = re.compile(r"faster payment|cash|liquidit|zahlungsziel", re.I)
FREEZE_RE = re.compile(r"frozen|freeze|cannot give up margin|no price reduction", re.I)


def offered(events):
    """Every term sheet in the supplier's latest message: its standing offer and the structured
    alternatives it lists ("Alternatively, with ... days net: ...")."""
    out = []
    for ts in latest(events).get("term_sheets") or []:
        if ts.get("term_sheet_id") and ts.get("terms"):
            out.append((ts["term_sheet_id"], ts["terms"]))
    return out


def supplier_reference(case, events):
    """The supplier's own terms from its opening quote PDF: what it offered (except the price),
    its standard terms and the terms fixed by the enquiry; the buyer's plan fills any gap.
    Used both to fill a term sheet's gaps and as the supplier's reference for its cash value."""
    q = {}
    for msg in events:
        for att in msg.get("attachments") or []:
            if att.get("name", "").startswith("quote") and att.get("text"):
                q = quote_terms(att["text"])
                break
        if q:
            break
    ref = {**q.get("fixed", {}), **q.get("standard", {}),
           **{k: v for k, v in q.get("stated", {}).items() if k != "unit_price"}}
    plan = eco.closing_terms(case, {"unit_price": 0.0}, ref)
    plan.pop("unit_price", None)
    return {**plan, **ref} if ref else (standard_terms(events) or plan)


class PegasusAgent:
    def __init__(self, package=None, playbook=None, push_after_final=False):
        self.pkg, self.playbook, self.push_after_final = package, playbook or {}, push_after_final
        self.case_id, self.claims, self.state = None, None, {}

    # -- per case / per thread state ----------------------------------------------------------
    def _prepare(self, case):
        self.case_id, self.state = case["case_id"], {}
        # Codex's dossier-based claims (all package suppliers, final included); ours as fallback
        self.intel = getattr(self, "intel", None) or (Intelligence(self.pkg) if self.pkg else None)
        self.claims = IntelClaims(case, self.pkg, self.intel) if self.intel else Claims(case, self.pkg)
        self.fallback_claims = Claims(case, self.pkg)
        if self.claims is not self.fallback_claims and hasattr(self.claims, "used"):
            self.fallback_claims.used = self.claims.used  # one ledger: an ID is cited once per case
        self.c_alt = eco.alt_cost(case)

    def _thread(self, case, sid, events):
        st = self.state.get(sid)
        if st is None:
            std = supplier_reference(case, events)
            first = next((m for m in events if m.get("from") != "buyer"), {})
            sheets = first.get("term_sheets") or []
            open_terms = dict(sheets[0]["terms"]) if sheets else {}
            st = self.state[sid] = {"std": std, "open": open_terms, "offers": 0, "seen": [],
                                    "drivers": " ".join(m.get("body", "") for m in events if m.get("from") != "buyer")}
            if getattr(self, "intel", None):
                try:
                    kinds = {x["type"] for x in self.intel.brief(case, sid).get("signals", [])}
                except Exception as e:  # intelligence is a bonus, never a reason to fail a case
                    log.warning("%s intel brief failed: %s", sid, e)
                    kinds = set()
                st["cash"] = "cash_sensitive" in kinds
                st["freeze"] = "price_frozen" in kinds
                st["signals"] = kinds
            st["anchor"] = self._anchor(case, sid, st)
        return st

    def _ref_terms(self, case, st):
        return eco.sheet(case, {k: v for k, v in eco.closing_terms(case, st["open"], st["std"]).items()
                                if k in eco.fields(case)}, st["open"].get("unit_price") or 0)

    def _anchor(self, case, sid, st):
        """Opening target C: the playbook's target, else a firm price on the best structure."""
        b = case["buyer"]
        ref = self._ref_terms(case, st)
        ranked = eco.structures(case, ref, st["std"], indifferent=RANK_INDIFFERENT)
        st["ranked"] = ranked
        if not ranked:
            return None
        best_c, best_s, best_p = ranked[0]
        target = (self.playbook.get(case["case_id"]) or {}).get("target_c")
        if target:
            c_open = eco.standing_cost(case, st["open"], st["std"])
            return target - 0.35 * max(c_open - target, 0)
        open_p = st["open"].get("unit_price") or best_p
        if case["type"] == "price_increase" and b.get("current_unit_price"):
            p = min(open_p * 0.92, b["current_unit_price"] * PI_ANCHOR)
        else:
            p = open_p * ANCHOR_DROP
        # the structure is chosen at the price we will ask, not at the supplier's: at its price a
        # short contract plus fallback can look cheaper (P08) while at ours full coverage wins
        best = eco.best_at_price(case, p, st["std"])
        return best[0] if best else eco.cost(case, eco.sheet(case, best_s, p), st["std"])

    # -- decision ------------------------------------------------------------------------------
    def decide(self, case, threads, sid=None, busy=()):
        if case["case_id"] != self.case_id:
            self._prepare(case)
        live = live_threads(threads)
        if not live:
            return None
        sid = sid if sid in live else live[0]
        events = threads[sid]
        st = self._thread(case, sid, events)
        last = latest(events)
        status = last.get("status")
        sheet_id, terms = standing(events)
        offers = [eco.standing_cost(case, t, st["std"]) for _, t in offered(events) if eco.ok(case, t, st["std"])]
        c_s = min(offers) if offers else float("inf")
        if (terms or {}).get("unit_price"):
            st["ask"] = terms["unit_price"]
            st["last_terms"] = terms
        if c_s == float("inf"):
            # nothing valid on the table (e.g. its quote breaks a must-have): concede towards what
            # our best structure would cost at its price, never towards infinity
            ranked = st.get("ranked") or []
            price = (terms or {}).get("unit_price") or st["open"].get("unit_price")
            if ranked and price:
                c_s = eco.cost(case, eco.sheet(case, ranked[0][1], price), st["std"])
        if sheet_id and c_s < float("inf"):
            st["seen"].append(c_s)

        text = last.get("body", "")
        if CASH_RE.search(text):
            st["cash"] = True
        if FREEZE_RE.search(text):
            st["freeze"] = True

        target_c = (self.playbook.get(case["case_id"]) or {}).get("target_c")
        good_enough = target_c is not None and c_s <= target_c + 1e-6 + 0.004 * max(
            eco.standing_cost(case, st["open"], st["std"]) - target_c, 0)

        if good_enough:
            return self._accept_best(case, threads, prefer=sid, busy=busy)
        if status == "agreed":
            # It took our sheet: if that came within our first two offers we asked too little -
            # push once more from a lower anchor while rounds remain; else take it.
            behind_target = target_c is not None and c_s > target_c
            if (st["offers"] <= 2 or behind_target) and not st.get("pushed_agreed") and moves_left(case, events) > 3:
                st["pushed_agreed"] = True
                c_open = eco.standing_cost(case, st["open"], st["std"])
                st["anchor"] = min(st["anchor"] or c_s, c_s) - 0.5 * max(c_open - c_s, 0)
                st["offers"] = 0
            else:
                return self._done(case, threads, sid, st, busy)
        if status == "final_offer" and not (self.push_after_final and not st.get("pushed")):
            return self._done(case, threads, sid, st, busy)
        if moves_left(case, events) <= 1:
            return self._done(case, threads, sid, st, busy)
        if (len(st["seen"]) >= 3 and st["offers"] >= 3 and st["seen"][-1] >= st["seen"][-2] - 1e-6
                and st["seen"][-2] >= st["seen"][-3] - 1e-6):
            return self._done(case, threads, sid, st, busy)  # it stopped moving
        if status == "final_offer":
            st["pushed"] = True

        k = min(st["offers"], len(SCHEDULE) - 1)
        anchor = st["anchor"] if st["anchor"] is not None else c_s
        target = anchor + SCHEDULE[k] * max(c_s - anchor, 0)
        prev = st.get("last_target")
        if prev is not None and k > 0:
            # suppliers answer "you only moved by one cent": every new offer moves visibly
            step = max(MIN_STEP * (c_s - prev), 0)
            target = max(target, min(prev + step, c_s))
        if target_c is not None:
            target = min(target, target_c) if moves_left(case, events) > 3 else target
        if target >= c_s * (1 - 1e-4):
            return self._done(case, threads, sid, st, busy)
        st["offers"] += 1
        st["last_target"] = target
        msg = self._message(case, threads, sid, st, target, k)
        alt = (case["buyer"].get("alternative") or {}).get("terms") or {}
        session_offers = {ts["term_sheet_id"] for ev in threads.values() for m in ev
                          for ts in (m.get("term_sheets") or []) if ts.get("term_sheet_id")}
        msg, problems = guard.check(case, msg, self.pkg.facts if self.pkg else {}, session_offers,
                                    ask=st.get("ask"), alt_price=alt.get("unit_price"))
        for p in problems:
            log.warning("%s %s guard: %s", case["case_id"], sid, p)
        return {"supplier_id": sid, "message": msg}

    def _done(self, case, threads, sid, st, busy):
        """This thread has gone as far as it will. In an RFQ, keep it alive with a question while
        another bidder is still moving (its offer is our best lever on them); close when no
        thread can still improve."""
        st["done"] = True
        live = live_threads(threads)
        still_moving = [s for s in live if s != sid and not (self.state.get(s) or {}).get("done")
                        and moves_left(case, threads[s]) > 1]
        if still_moving and moves_left(case, threads[sid]) > 2:
            return {"supplier_id": sid, "message": {
                "subject": f"Re: {case['title']}",
                "body": "Thank you for your offer. We are completing our evaluation of all offers and "
                        "will come back to you shortly. Could you confirm your earliest lead time?"}}
        return self._accept_best(case, threads, prefer=sid, busy=busy)

    def _accept_best(self, case, threads, prefer=None, busy=()):
        """Accept the cheapest standing sheet over all live threads if it keeps the must-haves and
        beats the alternative; else walk away."""
        best = None
        for s in live_threads(threads):
            st = self.state.get(s) or {"std": standard_terms(threads[s])}
            for sid_sheet, terms in offered(threads[s]):
                if not eco.ok(case, terms, st["std"]):
                    continue
                c = eco.standing_cost(case, terms, st["std"])
                if best is None or c < best[0] - 1e-6 or (abs(c - best[0]) < 1e-6 and s == prefer):
                    best = (c, s, sid_sheet)
        if best and best[1] in busy:
            # the best sheet is on a thread whose reply is on its way: wait for it, do not take a
            # dearer one meanwhile (and do not accept a sheet that may be stale)
            return {"wait": True}
        if best and best[0] < self.c_alt:
            log.info("%s: accept %s at C=%.0f", case["case_id"], best[2], best[0])
            return {"supplier_id": best[1], "message": {"action": {"type": "accept", "term_sheet_id": best[2]}}}
        if len(live_threads(threads)) > 1 and prefer:
            # RFQ: this thread is not good enough, keep the others going - a question keeps it alive
            return {"supplier_id": prefer, "message": {"subject": f"Re: {case['title']}",
                                                       "body": "Thank you. Could you confirm your earliest lead time?"}}
        log.info("%s: walk away (best %s, alternative %.0f)", case["case_id"], best, self.c_alt)
        return {"supplier_id": prefer or live_threads(threads)[0], "message": {"action": {"type": "walk_away"}}}

    # -- the e-mail ----------------------------------------------------------------------------
    def _sheets(self, case, st, target):
        current = {k: v for k, v in eco.closing_terms(case, st.get("last_terms") or st["open"], st["std"]).items()
                   if k in eco.fields(case)}
        picks = eco.best_for_target(case, target, st["std"], current, st.get("ask"))
        if picks:
            out = []
            if st.get("cash") and "payment_days" in picks[0][1] and case.get("term_options", {}).get("payment_days"):
                quick = {**picks[0][1], "payment_days": min(case["term_options"]["payment_days"])}
                if quick not in [s for _, s in picks]:
                    picks = picks[:2] + [(None, quick)]
            for p, s in picks[:3]:
                price = p if p is not None else min(eco.price_for(case, s, target, st["std"]), st.get("ask") or 1e18)
                t = eco.sheet(case, s, price)
                if eco.ok(case, t, st["std"]) and t not in out:
                    out.append(t)
            return out
        ranked = st.get("ranked") or []
        if not ranked:
            return []
        picks = [ranked[0][1]]
        main = ranked[0][1]
        for _, s, _ in ranked[1:]:  # alternatives that differ from the main structure in one lever
            if len(picks) >= 3:
                break
            diff = [f for f in s if s[f] != main[f]]
            if len(diff) == 1 and all(len([f for f in s if s[f] != p[f]]) >= 1 for p in picks):
                picks.append(s)
        if st.get("cash") and "payment_days" in main:
            faster = sorted(case["term_options"].get("payment_days", []))
            quick = {**main, "payment_days": faster[0] if faster else main["payment_days"]}
            if quick not in picks:
                picks.insert(1, quick)
        out = []
        cap = st.get("ask") or float("inf")
        for s in picks[:3]:
            p = min(eco.price_for(case, s, target, st["std"]), cap)
            t = eco.sheet(case, s, p)
            if eco.ok(case, t, st["std"]) and t not in out:
                out.append(t)
        return out

    def _message(self, case, threads, sid, st, target, k):
        sheets = self._sheets(case, st, target)
        part = case["parts"][0]
        price = (standing(threads[sid])[1] or {}).get("unit_price") or st["open"].get("unit_price")
        rivals = []
        for other in threads:
            if other == sid:
                continue
            oid, ot = standing(threads[other])
            if oid and ot.get("unit_price") and price and ot["unit_price"] < price:
                rivals.append((oid, ot["unit_price"]))
        rivals = sorted(rivals, key=lambda r: r[1])[:1]
        # a rival's offer in our own words (sandbox D02: 0.723 vs 0.702 with the dossier wording),
        # then the dossier's data claims, then ours as fallback - three at most
        chosen = self.fallback_claims.pick(sid, part, price, rivals=rivals, rivals_only=True) if rivals else []
        try:
            if self.claims is not self.fallback_claims:
                chosen += self.claims.pick(sid, part, price, st["drivers"])[: 3 - len(chosen)]
        except Exception as e:
            log.warning("%s intel claims failed: %s", sid, e)
        if len(chosen) < 3:
            chosen += self.fallback_claims.pick(sid, part, price, st["drivers"], limit=3 - len(chosen))
        lines = ["Dear Sir or Madam,", "", f"thank you for your offer regarding {case['title'].lower()}."]
        lines += [c["sentence"] for c in chosen]
        levers = [name for f, name in (("payment_days", "payment"), ("volume_per_year", "volume"),
                                       ("contract_months", "contract length"), ("effective_date", "timing"),
                                       ("chargeback_eur_per_complaint", "chargebacks"),
                                       ("ppm_commitment", "quality commitment"), ("lead_time_days", "lead time"),
                                       ("index_clause", "an index clause")) if f in case["negotiable"]]
        if st.get("freeze") and levers:
            lines.append("We understand your position on list prices, so we have structured the packages "
                         f"below around {', '.join(levers[:3])}.")
        if st.get("cash") and "payment_days" in case["negotiable"]:
            lines.append("We are happy to pay faster if that helps your cash flow, in exchange for a better price.")
        if len(sheets) > 1:
            lines.append("Each of the attached packages works for us; please choose the one that suits you best.")
        if k == 0:
            lines.append("Could you send us a cost breakdown for this part?")
        lines += ["We value our cooperation and would like an agreement that covers our full need.", "",
                  "Best regards", "Hollerau Pumpenwerke GmbH, Purchasing"]
        refs = [r for c in chosen for r in c["refs"]]
        msg = {"subject": f"Re: {case['title']}", "body": "\n".join(lines),
               "term_sheets": [{"terms": t} for t in sheets]}
        if refs:
            msg["refs"] = refs[:20]
        return msg
