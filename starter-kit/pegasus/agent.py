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
from . import adapt as rules
from . import guard, memory, prose, reader

try:  # Nevil's evidence lane (hollerau/evidence, PR #4): filtered claim union + planner hints
    from hollerau.evidence import adapter as team_claims
except Exception:  # pragma: no cover - the package is optional
    team_claims = None
TEAM_CLAIMS = os.environ.get("PEGASUS_TEAM_CLAIMS", "1") == "1" and team_claims is not None
from .claims import Claims
from .claims_intel import Claims as IntelClaims
from .intel_bridge import Intelligence

log = logging.getLogger("pegasus")

# Tunables (environment, for the sandbox bench): structure ranking and opening anchor.
RANK_INDIFFERENT = os.environ.get("PEGASUS_RANK", "fixed") == "indifferent"
ANCHOR_DROP = float(os.environ.get("PEGASUS_ANCHOR", "0.80"))      # share of the supplier's price
PI_ANCHOR = float(os.environ.get("PEGASUS_PI_ANCHOR", "0.94"))     # price increase: share of current
MIN_STEP = float(os.environ.get("PEGASUS_MIN_STEP", "0.15"))  # share of the gap to their offer
EXPECT = float(os.environ.get("PEGASUS_EXPECT", "0.67"))     # where between our anchor and its ask we expect to sign
ANCHOR_KEEP = float(os.environ.get("PEGASUS_ANCHOR_KEEP", "0.95"))
ANCHOR_FLOOR = float(os.environ.get("PEGASUS_ANCHOR_FLOOR", "0.945"))  # relationship cases: anchor >= this x floor (0.97 opened P01 at 129: 0.973)  # keep the anchor structure within this share of the best price
FINAL_TRADE = os.environ.get("PEGASUS_FINAL_TRADE", "0") == "1"
# Research-backed message tactics (sandbox bench decides which stay on)
ESCALATE = os.environ.get("PEGASUS_ESCALATE", "0") == "1"      # ask its management early (PON)
CONDITIONAL = os.environ.get("PEGASUS_COND", "0") == "1"       # "if you can do X, we commit to Y"
WARM = os.environ.get("PEGASUS_WARM", "0") == "1"              # specific thanks + open question (MIT)
MAX_CLAIMS = int(os.environ.get("PEGASUS_MAX_CLAIMS", "3"))    # fewer, stronger arguments (Maaravi)
CASH_PREMIUM = float(os.environ.get("PEGASUS_CASH_PREMIUM", "0"))  # faster payment costs it extra
GAP_LINE = os.environ.get("PEGASUS_GAP", "0") == "1"           # RFQ: tell each bidder its gap in %
NONCE = os.environ.get("PEGASUS_NONCE", "")  # bench only: a run tag in the subject defeats the reply cache
TFT = float(os.environ.get("PEGASUS_TFT", "0"))          # >0: reciprocal steps (x supplier's last move)
MIN_TFT = float(os.environ.get("PEGASUS_MIN_TFT", "0.05"))  # plus this share of the gap, so we always move
# Three independent experiments; setting all three to 0 reproduces r9.
CAPACITY_ANCHOR = float(os.environ.get("PEGASUS_CAPACITY_ANCHOR", "0.94"))
APPROVAL_REVIEW = os.environ.get("PEGASUS_APPROVAL_REVIEW", "1") == "1"
RFQ_BAFO = os.environ.get("PEGASUS_RFQ_BAFO", "0") == "1"
# Share of the remaining gap (anchor -> supplier's standing C) we have conceded after k offers.
SCHEDULE = [float(x) for x in os.environ.get(
    "PEGASUS_SCHEDULE", "0,0.22,0.38,0.52,0.63,0.72,0.80,0.86,0.90,0.93").split(",")]
# Off by default: on the sandbox (D02) walking up from the direct cost scored 0.534 vs 0.702.
USE_FLOOR = os.environ.get("PEGASUS_FLOOR", "0") == "1"
FLOOR_MARGIN = float(os.environ.get("PEGASUS_FLOOR_MARGIN", "0.015"))  # over direct cost
FLOOR_STEP = float(os.environ.get("PEGASUS_FLOOR_STEP", "0.008"))      # per offer once known
OVERHEAD_RE = re.compile(r"overhead|profit|margin|gewinn|marge", re.I)
CASH_RE = re.compile(r"faster payment|cash|liquidit|zahlungsziel", re.I)
FREEZE_RE = re.compile(r"frozen|freeze|cannot give up margin|no price reduction", re.I)
INTERNAL_RE = re.compile(r"clarify.{0,30}internally|reviewed internally|approval|corporate policy|company policy", re.I)


def direct_cost(events):
    """Direct cost per unit from the latest cost breakdown a supplier attached: the sum of its
    elements except overhead and profit. In the sandbox the best price the supplier would accept
    sits about 2 % above it (D02: 12.76 direct, C_best at 13.06)."""
    for msg in reversed(events):
        for att in msg.get("attachments") or []:
            if "cost" not in att.get("name", "").lower() or not att.get("text"):
                continue
            total, n = 0.0, 0
            for line in att["text"].splitlines():
                cells = [c.strip() for c in line.split("|")]
                if len(cells) < 2 or cells[0].lower() in ("element", "unit price") or OVERHEAD_RE.search(cells[0]):
                    continue
                try:
                    total += float(cells[1]); n += 1
                except ValueError:
                    continue
            if n:
                return total
    return None


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
        self.refused, self.mirrored = set(), set()  # sheet IDs the arena refused / we sent back
        self._observed = {}  # (supplier, text) -> signals read before the decision lock
        self.case_id, self.claims, self.state = None, None, {}

    def _c_open(self, case, threads):
        """C of the cheapest opening quote that keeps the must-haves (the score's zero point)."""
        out = float("inf")
        for s, events in threads.items():
            first = next((m for m in events if m.get("from") != "buyer"), {})
            st = self.state.get(s) or {"std": supplier_reference(case, events)}
            for ts in (first.get("term_sheets") or [])[:1]:
                if ts.get("terms", {}).get("unit_price") and eco.ok(case, ts["terms"], st["std"]):
                    out = min(out, eco.standing_cost(case, ts["terms"], eco.with_relationship(st["std"])))
        return out

    def closing(self, case, threads):
        """Before the wave's cap: accept the cheapest standing sheet by OUR cost (relationship
        charge included, standard terms filled) if it beats the alternative and the opening,
        else walk away - the kit's closing_move ignores the relationship charge."""
        if case["case_id"] != self.case_id:
            self._prepare(case)
        best = None
        for s in live_threads(threads):
            st = self.state.get(s) or self._thread(case, s, threads[s])
            sheets = offered(threads[s])[:1]  # only the standing sheet can be accepted
            for sid_sheet, terms in sheets:
                if sid_sheet in self.refused or not eco.ok(case, terms, st["std"]):
                    continue
                c = eco.standing_cost(case, terms, eco.with_relationship(st["std"]))
                if best is None or c < best[0]:
                    best = (c, s, sid_sheet)
        live = live_threads(threads)
        if best and best[0] < min(self.c_alt, self._c_open(case, threads)):
            return {"supplier_id": best[1], "message": {"action": {"type": "accept", "term_sheet_id": best[2]}}}
        if best and self.c_alt == float("inf"):
            # no alternative: a deal at the opening still scores what no deal scores, and wins ties
            return {"supplier_id": best[1], "message": {"action": {"type": "accept", "term_sheet_id": best[2]}}}
        return {"supplier_id": live[0], "message": {"action": {"type": "walk_away"}}} if live else None

    def observe(self, sid, message):
        """Read a supplier reply (an LLM call) outside the decision lock; `decide` finds it here."""
        text = (message or {}).get("body") or ""
        if text and (message or {}).get("from") != "buyer":
            try:
                self._observed[(sid, text[:200])] = reader.read(text)
            except Exception as e:  # reading is a bonus, never a reason to lose the round
                log.warning("observe failed: %s", e)

    def finish(self, case, threads, result=None):
        """The case ended: keep what every supplier taught us."""
        for sid, st in self.state.items():
            try:
                memory.remember(sid, st, case, result)
            except Exception as e:
                log.warning("memory failed for %s: %s", sid, e)

    def refuse(self, sheet_id):
        """The arena refused to close on this sheet: never try it again."""
        self.refused.add(sheet_id)

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
            flags, stated = memory.prior_flags(sid, case)  # what this supplier taught us before
            if flags or stated:
                st.update(flags)
                st.setdefault("stated", {}).update(stated)
                log.info("%s %s: remembered %s %s", case["case_id"], sid, sorted(flags), stated)
            sup = eco.supplier_model(case, st["std"], st["open"].get("unit_price"),
                                     self.pkg.facts if self.pkg else {})
            if sup:
                st["std"]["_floor"] = sup  # floors our prices; counted in C only to decide acceptance
                log.info("%s %s: supplier full cost estimate %.2f", case["case_id"], sid, sup["full_cost"])
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
        # Asking simultaneously for more capacity and earlier delivery calls for a credible
        # first price. The public opening identifies both constraints; no supplier ID is used.
        volume = st["open"].get("volume_per_year")
        lead = st["open"].get("lead_time_days") or st["std"].get("lead_time_days")
        if (CAPACITY_ANCHOR > 0 and b.get("category_goal") == "secure_supply"
                and {"volume_per_year", "lead_time_days"} <= set(eco.fields(case))
                and volume and b["need_per_year"] > volume
                and lead and lead > b["need_lead_time_days"]):
            st["capacity_pressure"] = True
            p = max(p, open_p * min(CAPACITY_ANCHOR, 1.0))
        # the structure is chosen at the price the deal will likely close at (anchor + EXPECT of the
        # gap to its ask): at its own price a short contract plus fallback looks cheaper (P08), at our
        # anchor a November start looks cheaper (P01) - neither is where we will sign
        expected = p + EXPECT * max(open_p - p, 0)
        cur = {f: v for f, v in ref.items() if f in eco.fields(case)}
        best = eco.best_at_price(case, expected, st["std"], cur)
        if best:
            st["anchor_structure"] = best[1]
            if st["std"].get("_floor"):  # strategic supplier: never open far under its cost (P02 lot A scored 0)
                p = max(p, ANCHOR_FLOOR * eco.price_floor(case, best[1], st["std"], max(10 * open_p, 1.0)))
            return eco.cost(case, eco.sheet(case, best[1], p), st["std"])
        return eco.cost(case, eco.sheet(case, best_s, p), st["std"])

    # -- decision ------------------------------------------------------------------------------
    def decide(self, case, threads, sid=None, busy=()):
        out = self._decide(case, threads, sid, busy)
        if out and isinstance(out.get("message"), dict) and not out["message"].get("action") and out["message"].get("body"):
            who = out.get("supplier_id")
            n = sum(m.get("from") == "buyer" for m in threads.get(who, [])) + 1
            subj = out["message"].get("subject") or f"Re: {case['title']}"
            out["message"]["subject"] = f"{subj} ({n})"[:300]
        return out

    def _decide(self, case, threads, sid=None, busy=()):
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
        # read what the supplier said into signals once per reply (LLM with verified quotes, else keywords)
        key = (len(events), text[:80])
        if text and st.get("read_key") != key:
            st["read_key"] = key
            cached = self._observed.pop((sid, text[:200]), None)
            st["last_signals"] = cached if cached is not None else reader.read(text)
            reader.apply(st, st["last_signals"])
            if "cost_floor" in st["last_signals"]:
                st["floor_hits"] = st.get("floor_hits", 0) + 1
            if st["last_signals"]:
                log.info("%s %s heard: %s", case["case_id"], sid, sorted(st["last_signals"]))
        if rules.must_close(st, case) and status != "agreed" and sheet_id:
            return self._done(case, threads, sid, st, busy)

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
            # pushing after a final offer made P02 walk away (lot B): off unless PEGASUS_FINAL_TRADE=1
            trade = self._fresh_final_trade(case, threads, sid, st, c_s, busy) if FINAL_TRADE else None
            if trade:
                target, sheet = trade
                st["final_trade_sent"] = True
                st["offers"] += 1
                st["last_target"] = target
                return self._send(case, threads, sid, st, target, st["offers"] - 1,
                                  sheets=[sheet], final_trade=True)
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
        if c_s <= anchor + 1e-6 and sheet_id:
            # its standing offer already costs us no more than our opening demand: take it
            return self._done(case, threads, sid, st, busy)
        target = anchor + SCHEDULE[k] * max(c_s - anchor, 0)
        if st.get("last_target") is not None:
            target = max(target, st["last_target"])  # concessions only go one way (P02/P01 lot E retreated)
        dc = direct_cost(events) if USE_FLOOR else None
        if dc and not st.get("floor_price"):
            st["floor_price"], st["floor_at"] = dc * (1 + FLOOR_MARGIN), st["offers"]
            log.info("%s %s: cost breakdown, direct cost %.2f", case["case_id"], sid, dc)
        if st.get("floor_price") and moves_left(case, events) > 2:
            # we know its cost: walk up from just above it, slowly, instead of splitting the gap
            p = st["floor_price"] * (1 + FLOOR_STEP * (st["offers"] - st["floor_at"]))
            best = eco.best_at_price(case, p, st["std"])
            if best:
                target = min(target, best[0])
        prev = st.get("last_target")
        if TFT > 0 and prev is not None and k > 0 and len(st["seen"]) >= 2:
            # reciprocity (MiCRO, de Jonge 2022): move by what the supplier just moved, no more
            their_move = max(st["seen"][-2] - st["seen"][-1], 0.0)
            target = min(prev + TFT * their_move + MIN_TFT * max(c_s - prev, 0), c_s)
        elif prev is not None and k > 0 and not st.get("floor_price"):
            # suppliers answer "you only moved by one cent": every new offer moves visibly
            step = max(MIN_STEP * (c_s - prev), 0)
            target = max(target, min(prev + step, c_s))
        if target_c is not None:
            target = min(target, target_c) if moves_left(case, events) > 3 else target
        if target >= c_s * (1 - 1e-4):
            return self._done(case, threads, sid, st, busy)
        st["offers"] += 1
        st["last_target"] = target
        return self._send(case, threads, sid, st, target, k, busy=busy)

    def _floored(self, case, st, msg):
        """Never offer under the price where the relationship charge outgrows the saving."""
        if not st["std"].get("_floor") or st.get("offers", 0) < 3:
            return msg
        for ts in msg.get("term_sheets") or []:
            t = ts["terms"]
            s = {f: v for f, v in t.items() if f in eco.fields(case)}
            floor = eco.price_floor(case, s, st["std"], max(10 * t["unit_price"], 1.0))
            if t["unit_price"] < floor:
                t["unit_price"] = round(min(floor, st.get("ask") or floor), 2)
        return msg

    def _send(self, case, threads, sid, st, target, k, sheets=None, final_trade=False, busy=()):
        msg = self._floored(case, st, self._message(case, threads, sid, st, target, k, sheets, final_trade))
        now = [ts["terms"] for ts in msg.get("term_sheets") or []]
        if now and now == st.get("last_sheets"):
            # every sheet is capped (floor or ask). While the supplier keeps moving, holding firm is
            # the right move (P02 lot D: it came down to 358.95 against our floor); when it stalls
            # too, repeating only irritates it ("you repeat your figures", P01) - close.
            moved = len(st["seen"]) >= 2 and st["seen"][-1] < st["seen"][-2] - 1e-6
            if not moved:
                log.info("%s %s: sheets would repeat and it stopped moving - closing", case["case_id"], sid)
                return self._done(case, threads, sid, st, busy)
            for t in now:  # a token step so the message is not a verbatim repeat
                t["unit_price"] = round(min(t["unit_price"] * 1.002, st.get("ask") or 1e18), 2)
        st["last_sheets"] = [dict(t) for t in now]
        for ts in msg.get("term_sheets") or []:  # what we have offered so far (for `remaining_trade`)
            st.setdefault("proposed", []).append({f: v for f, v in ts["terms"].items() if f != "unit_price"})
        if prose.ENABLED and msg.get("body"):
            lines = msg["body"].split("\n")
            args = [l for l in lines if re.search(r"\b(?:idx|quote|offer|sc|qn|po|cost|fc|news|doc|ctr):", l)]
            extra = [l for l in lines if l and l not in args and not l.startswith(("Dear", "Best regards", "Hollerau"))]
            body = prose.narrate(latest(threads[sid]).get("body", ""), msg["body"], args, extra)
            if body:
                candidate = {**msg, "body": body}
                alt = (case["buyer"].get("alternative") or {}).get("terms") or {}
                offers = {ts["term_sheet_id"] for ev in threads.values() for m in ev
                          for ts in (m.get("term_sheets") or []) if ts.get("term_sheet_id")}
                checked, problems = guard.check(case, candidate, self.pkg.facts if self.pkg else {}, offers,
                                                ask=st.get("ask"), alt_price=alt.get("unit_price"))
                if not problems:
                    msg = checked
                else:
                    log.warning("%s %s prose rejected by the guard: %s", case["case_id"], sid, problems[:2])
        alt = (case["buyer"].get("alternative") or {}).get("terms") or {}
        session_offers = {ts["term_sheet_id"] for ev in threads.values() for m in ev
                          for ts in (m.get("term_sheets") or []) if ts.get("term_sheet_id")}
        msg, problems = guard.check(case, msg, self.pkg.facts if self.pkg else {}, session_offers,
                                    ask=st.get("ask"), alt_price=alt.get("unit_price"))
        for p in problems:
            log.warning("%s %s guard: %s", case["case_id"], sid, p)
        for ts in msg.get("term_sheets", []):
            structure = {f: ts["terms"].get(f) for f in eco.fields(case)}
            history = st.setdefault("proposed_structures", [])
            if structure not in history:
                history.append(structure)
        return {"supplier_id": sid, "message": msg}

    def _index_preference(self, case, events):
        """A mentioned allowed family; a requirement also needs a structured supplier clause."""
        if "index_clause" not in case["negotiable"]:
            return None, False
        text = latest(events).get("body", "").lower()
        options = [c for c in case.get("term_options", {}).get("index_clause", []) if c]
        families = {c["series"] for c in options}
        std = supplier_reference(case, events)
        structured = [(t.get("index_clause") or {}).get("series") for _, t in offered(events)
                      if eco.ok(case, t, std)]
        required = bool(re.search(r"must (?:\w+\W+){0,3}(?:include|apply|maintain|insist)|mandatory|strict requirement|cannot (?:fix|offer fixed)", text))
        if re.search(r"not mandatory|not required|do not require|need not (?:include|apply)", text):
            required = False
        series = next((s for s in sorted(families) if s.lower() in text and s in structured), None)
        if series:
            return series, required
        return next((s for s in sorted(families) if s.lower() in text), None), False

    def _fresh_final_trade(self, case, threads, sid, st, c_s, busy):
        """One conditional give/get, with rounds to receive a reply and close.

        Do not disturb RFQ coordination, an agreement, a better priced alternative or an
        explicit playbook. A new price on a previously offered structure is not a new trade.
        """
        events = threads[sid]
        if (st.get("final_trade_sent") or busy or len(live_threads(threads)) != 1
                or moves_left(case, events) <= 2 or st.get("last_target") is None
                or self.playbook.get(case["case_id"])):
            return None
        _, current = standing(events)
        if not current or not eco.ok(case, current, st["std"]):
            return None
        primary_c = eco.standing_cost(case, current, st["std"])
        if c_s < primary_c - 0.01:  # mirror the supplier's better alternative first
            return None
        text = latest(events).get("body", "").lower()
        requested = set()
        if CASH_RE.search(text): requested.add("payment_days")
        if re.search(r"longer|extended|planning certainty", text): requested.add("contract_months")
        if re.search(r"higher volume|larger volume", text): requested.add("volume_per_year")
        if re.search(r"overtime|premium|expedited", text): requested.add("lead_time_days")
        if re.search(r"ppm|quality|chargeback|complaint", text):
            requested |= {"ppm_commitment", "chargeback_eur_per_complaint"}
        series, required = self._index_preference(case, events)
        if not series:
            series, required = st.get("index_preference", (None, False))
        if series: requested.add("index_clause")
        requested &= set(eco.fields(case))
        if not requested:
            return None
        # A visible late concession, retaining MIN_STEP of the remaining buyer gain.
        target = c_s - MIN_STEP * max(c_s - st["last_target"], 0)
        if target >= c_s - 0.01:
            return None
        previous = [{f: ts["terms"].get(f) for f in eco.fields(case)}
                    for ev in events if ev.get("from") == "buyer"
                    for ts in ev.get("term_sheets", [])]
        previous += st.get("proposed_structures", [])
        current = eco.closing_terms(case, current, st["std"])
        candidates = []
        for p, c, s in eco.priced_structures(case, target, st["std"], current, st.get("ask"), series, required):
            if s in previous or p < 0.9 * current["unit_price"]:
                continue
            give = {f for f in ("contract_months", "volume_per_year", "lead_time_days", "ppm_commitment")
                    if f in s and s[f] > current.get(f, s[f])}
            give |= {f for f in ("payment_days", "chargeback_eur_per_complaint")
                     if f in s and s[f] < current.get(f, s[f])}
            clause, old_clause = s.get("index_clause"), current.get("index_clause")
            if clause and (not old_clause or clause["deadband_pct"] < old_clause["deadband_pct"]):
                give.add("index_clause")
            if not give & requested or c >= c_s - 0.01:
                continue
            # A never-offered middle payment term is not fresh when faster payment was
            # already proposed. Require a concession beyond the previous offered envelope.
            fresh = set()
            for f in give & requested:
                values = [old[f] for old in previous if isinstance(old.get(f), (int, float))]
                if f == "index_clause":
                    bands = [old[f]["deadband_pct"] for old in previous if old.get(f)]
                    if not bands or clause["deadband_pct"] < min(bands): fresh.add(f)
                elif values:
                    if f in ("payment_days", "chargeback_eur_per_complaint"):
                        if s[f] < min(values): fresh.add(f)
                    elif s[f] > max(values): fresh.add(f)
                else:
                    fresh.add(f)
            if not fresh:
                continue
            value = eco.supplier_value(case, eco.sheet(case, s, p),
                                       eco.supplier_estimate(st["std"], case["buyer"]["interest_rate"]))
            candidates.append((value, p, c, s))
        if not candidates:
            return None
        _, p, _, structure = max(candidates, key=lambda r: (r[0], r[1], -r[2]))
        return target, eco.sheet(case, structure, p)

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
        best, alt_best = None, None
        for s in live_threads(threads):
            st = self.state.get(s) or {"std": standard_terms(threads[s])}
            for i, (sid_sheet, terms) in enumerate(offered(threads[s])):
                if sid_sheet in self.refused or not eco.ok(case, terms, st["std"]):
                    continue
                c = eco.standing_cost(case, terms, eco.with_relationship(st["std"]))
                if i > 0:
                    # an alternative ("offer:...-a") cannot be accepted - only the standing sheet can
                    if alt_best is None or c < alt_best[0]:
                        alt_best = (c, s, sid_sheet, terms)
                    continue
                if best is None or c < best[0] - 1e-6 or (abs(c - best[0]) < 1e-6 and s == prefer):
                    best = (c, s, sid_sheet)
        if alt_best and (best is None or alt_best[0] < best[0] - 1e-6):
            c, s, aid, terms = alt_best
            st = self.state.get(s) or {"std": standard_terms(threads[s])}
            if s not in busy and aid not in self.mirrored and moves_left(case, threads[s]) > 1:
                # send its own alternative back as our sheet: it will answer "agreed" with a
                # standing sheet we can accept (P10: offer:70410-1-a at 31.90 vs 33.80 standing)
                self.mirrored.add(aid)
                sheet = eco.sheet(case, {f: v for f, v in eco.closing_terms(case, terms, st["std"]).items()
                                         if f in eco.fields(case)}, terms["unit_price"])
                log.info("%s: mirror alternative %s", case["case_id"], aid)
                return {"supplier_id": s, "message": {
                    "subject": f"Re: {case['title']}",
                    "body": "Thank you for your offer. We are ready to accept your alternative package "
                            "as attached. Please confirm it.\n\nBest regards\nHollerau Pumpenwerke GmbH, Purchasing",
                    "term_sheets": [{"terms": sheet}]}}
        if best and best[1] in busy:
            # the best sheet is on a thread whose reply is on its way: wait for it, do not take a
            # dearer one meanwhile (and do not accept a sheet that may be stale)
            return {"wait": True}
        c_open = self._c_open(case, threads)
        final = best and latest(threads[best[1]]).get("status") in ("final_offer", "agreed")
        margin = 0.0 if final else 0.002  # after its last word, 'review your price' makes it walk (P02 lot E)
        if best and best[0] >= c_open - margin * abs(c_open):
            # a deal not clearly cheaper than the best opening scores 0 (P02 lot A: estimated
            # 582 EUR below the opening, 2 902 EUR above it in the arena) - keep negotiating
            log.info("%s: best %.0f not clearly below the opening %.0f", case["case_id"], best[0], c_open)
            if moves_left(case, threads[best[1]]) > 1 and best[1] not in busy:
                return {"supplier_id": best[1], "message": {"subject": f"Re: {case['title']}",
                        "body": "Thank you. Your offer does not yet improve on your opening quote for us. "
                                "Could you review your price?\n\nBest regards\nHollerau Pumpenwerke GmbH, Purchasing"}}
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
    def _prefer_anchor(self, case, st, target, picks):
        """The structure chosen at the expected price leads while its price at the target is within
        ANCHOR_KEEP of the best; it is inserted when the ranking left it out (P01: at a low target the
        November start carries a higher price than April, yet April is what the supplier grants)."""
        a = st.get("anchor_structure")
        if not a or not picks:
            return picks
        structures = [s for _, s in picks]
        if a in structures:
            i = structures.index(a)
            if i > 0 and picks[i][0] >= ANCHOR_KEEP * picks[0][0]:
                return [picks[i]] + picks[:i] + picks[i + 1:]
            return picks
        p = eco.price_for(case, a, target, st["std"])
        if st.get("ask"):
            p = min(p, st["ask"])
        if p >= ANCHOR_KEEP * picks[0][0] and eco.ok(case, eco.sheet(case, a, p), st["std"]):
            return [(p, a)] + picks[:2]
        return picks

    def _sheets(self, case, st, target):
        current = {k: v for k, v in eco.closing_terms(case, st.get("last_terms") or st["open"], st["std"]).items()
                   if k in eco.fields(case)}
        if rules.active(st):  # the supplier stated a constraint: honour it in every sheet
            picked = rules.pick_sheets(case, st, target, current, st.get("ask"))
            if picked:
                st["trade_offered"] = True
                return picked
        series, required = st.get("index_preference", (None, False))
        adapt = bool(series) or {"ppm_commitment", "chargeback_eur_per_complaint"} <= set(eco.fields(case))
        picks = self._prefer_anchor(case, st, target, eco.best_for_target(case, target, st["std"], current, st.get("ask"),
                                   adapt=adapt, index_series=series, require_index=required))
        if st.get("capacity_pressure") and picks:
            # Replace a cost-identical rush duplicate with a priced, feasible standard-lead
            # alternative. Keep the main package and the contract-length trade intact.
            main_p, main = picks[0]
            relaxed = eco.sheet(case, {**main, "lead_time_days": st["std"]["lead_time_days"]}, main_p)
            structure = {f: relaxed[f] for f in eco.fields(case)}
            price = min(eco.price_for(case, structure, target, st["std"]), st.get("ask") or 1e18)
            price = max(price, eco.price_floor(case, structure, st["std"], max(10 * price, 1.0)))
            relaxed = eco.sheet(case, structure, price)
            duplicate = next((i for i, (p, s) in enumerate(picks[1:], 1)
                if p == main_p and s.get("lead_time_days", 0) < main.get("lead_time_days", 0)
                and all(v == main.get(f) for f, v in s.items() if f != "lead_time_days")), None)
            if (relaxed["lead_time_days"] > main.get("lead_time_days", 0)
                    and price >= .9 * main_p and price <= (st.get("ask") or 1e18)
                    and eco.ok(case, relaxed, st["std"])
                    and eco.cost(case, relaxed, st["std"]) <= target + .01
                    and structure not in [s for _, s in picks]):
                if duplicate is not None:
                    picks[duplicate] = (price, structure)
                elif len(picks) < 3:
                    picks.append((price, structure))
        if adapt and not picks:
            return []  # do not relax an observed requirement or exceed C through the legacy fallback
        if picks:
            out = []
            if st.get("cash") and "payment_days" in picks[0][1] and case.get("term_options", {}).get("payment_days"):
                quick = {**picks[0][1], "payment_days": min(case["term_options"]["payment_days"])}
                if quick not in [s for _, s in picks]:
                    picks = picks[:2] + [(None, quick)]
            main_p = picks[0][0]
            for p, s in picks[:3]:
                price = p if p is not None else min(eco.price_for(case, s, target, st["std"]), st.get("ask") or 1e18)
                if (CASH_PREMIUM and main_p and "payment_days" in s and s is not picks[0][1]
                        and s["payment_days"] < picks[0][1].get("payment_days", 0) and price < main_p):
                    price = price - CASH_PREMIUM * (main_p - price)  # paying faster must buy more than its interest
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

    def _message(self, case, threads, sid, st, target, k, sheets=None, final_trade=False):
        preference = self._index_preference(case, threads[sid])
        if preference[0]:
            st["index_preference"] = preference
        sheets = self._sheets(case, st, target) if sheets is None else sheets
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
        chosen = None
        if TEAM_CLAIMS:
            try:  # the team's filters drop claims that work against us (volume to a saturated supplier, ...)
                cands = ([x for r in rivals for x in self.fallback_claims.rival_claim(*r)]
                         + self.fallback_claims.candidates(sid, part, price, st["drivers"]))
                picked = team_claims.pegasus_claims(case, self.pkg, cands, used=self.fallback_claims.used,
                                                    supplier=sid)[:rules.claims_cap(st, MAX_CLAIMS)]
                team_claims.remember(picked, sid, self.fallback_claims.used)
                chosen = [{"refs": list(c.ids), "sentence": c.sentence} for c in picked]
            except Exception as e:
                log.warning("%s team claims failed (%s) - ours instead", sid, e)
                chosen = None
        if chosen is None:
            chosen = self.fallback_claims.pick(sid, part, price, rivals=rivals, rivals_only=True) if rivals else []
            try:
                if self.claims is not self.fallback_claims:
                    chosen += self.claims.pick(sid, part, price, st["drivers"])[: 3 - len(chosen)]
            except Exception as e:
                log.warning("%s intel claims failed: %s", sid, e)
            if len(chosen) < 3:
                chosen += self.fallback_claims.pick(sid, part, price, st["drivers"], limit=3 - len(chosen))
        lines = ["Dear Sir or Madam,", "", f"thank you for your offer regarding {case['title'].lower()}."]
        prev_their = st["seen"][-2] if len(st["seen"]) >= 2 else None
        if WARM and prev_their is not None and st["seen"][-1] < prev_their:
            lines[-1] = ("thank you for your offer and for the step you made towards us on "
                         f"{case['title'].lower()} - we appreciate it.")
        cap = rules.claims_cap(st, MAX_CLAIMS)
        for c in chosen[cap:]:  # not sent: give their IDs back to the ledger
            self.fallback_claims.used -= set(c["refs"])
            self.fallback_claims.used.discard((sid, tuple(c["refs"])))
        chosen = chosen[:cap]
        lines += [c["sentence"] for c in chosen]
        lines += rules.lines(case, st, sheets)
        if not sheets:
            lines.append("Could you confirm a feasible package using the allowed terms?")
        if final_trade:
            lines.append("Thank you for your final offer. We can make the attached additional commitment "
                         "in exchange for the attached price. If no improvement is possible, "
                         "please keep your current offer unchanged so we can close on it.")
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
        elif st.get("cash") and case.get("fixed_terms", {}).get("payment_days") is not None:
            lines.append(f"The enquiry fixes payment at {case['fixed_terms']['payment_days']} days net; "
                         "the attached packages therefore trade only the negotiable terms.")
        if len(sheets) > 1:
            lines.append("Each of the attached packages works for us; please choose the one that suits you best.")
        # State the binding commitment the sheets carry - suppliers ask for it (P02: "Which volume
        # can you commit to?" four times, unanswered) - and answer its direct questions.
        main = sheets[0] if sheets else {}
        fixed = case.get("fixed_terms", {})
        vol = main.get("volume_per_year") or fixed.get("volume_per_year") or case["buyer"]["need_per_year"]
        months = main.get("contract_months") or fixed.get("contract_months")
        unit = "pcs"
        metadata = self.intel.pkg() if getattr(self, "intel", None) else None
        if metadata and part in metadata.ids:
            unit = metadata.ids[part][1].get("uom") or unit
            if unit == "pc": unit = "pcs"
        if len(case["parts"]) > 1:
            unit = "complete bundles of " + ", ".join(case["parts"])
        if sheets and (ESCALATE or CONDITIONAL):
            main_t = sheets[0]
            gives = []
            if main_t.get("contract_months"):
                gives.append(f"a {main_t['contract_months']}-month agreement")
            gives.append(f"{vol:,} {unit} per year")
            if CONDITIONAL:
                lines.append(f"If you can do EUR {main_t['unit_price']:.2f} per unit, we commit to "
                             f"{' and '.join(gives)} right away.")
            if ESCALATE and k <= 1:
                lines.append(f"Could you check with your management whether EUR {main_t['unit_price']:.2f} "
                             f"is possible if we commit to {' and '.join(gives)}?")
        if GAP_LINE and rivals and price:
            gap = (price / rivals[0][1] - 1) * 100
            if gap > 0.1:
                lines.append(f"On a comparable basis your offer is {gap:.1f}% above the best offer we hold.")
        if WARM:
            lines.append("What would help you move closer to these terms?")
        commit = f"We commit to a binding volume of {vol:,} {unit} per year"
        lines.append(commit + (f" over {months} months." if months else "."))
        asked = (latest(threads[sid]).get("body") or "").lower()
        if re.search(r"lead ?time|lieferzeit|delivery date", asked) and "?" in asked:
            lines.append(f"Our required lead time is {case['buyer']['need_lead_time_days']} days.")
        if k == 0:
            lines.append("Could you send us a cost breakdown for this part?")
        # Escalate on the current counter, before a final offer; no extra round is consumed.
        if (APPROVAL_REVIEW and sheets and not st.get("approval_requested")
                and latest(threads[sid]).get("status") == "open" and st["offers"] <= 2
                and moves_left(case, threads[sid]) > 2 and INTERNAL_RE.search(asked)):
            lines.append("Could your commercial manager review and approve your best price on the "
                         "attached terms, including our stated binding volume commitment?")
            st["approval_requested"] = True
        # A single BAFO on an existing counter, with a live, feasible rival quote. Agreed and
        # final offers are left alone, including the cheapest RFQ bid we already hold.
        viable_rival = RFQ_BAFO and any(other != sid and other in live_threads(threads)
            and standing(threads[other])[0]
            and (standing(threads[other])[1].get("unit_price") or 0) > 0
            and eco.ok(case, standing(threads[other])[1],
                (self.state.get(other) or {}).get("std") or supplier_reference(case, threads[other]))
            for other in threads)
        if (RFQ_BAFO and sheets and viable_rival and not st.get("bafo_requested")
                and latest(threads[sid]).get("status") == "open" and st["offers"] == 2
                and moves_left(case, threads[sid]) > 2):
            lines.append("We are comparing the live quotations for this enquiry. Please submit your "
                         "best and final terms on the attached packages and keep your current "
                         "quotation available until our award decision.")
            st["bafo_requested"] = True
        lines += ["We value our cooperation.", "",
                  "Best regards", "Hollerau Pumpenwerke GmbH, Purchasing"]
        refs = [r for c in chosen for r in c["refs"]]
        msg = {"subject": f"Re: {case['title']}" + (f" [{NONCE}]" if NONCE else ""), "body": "\n".join(lines),
               "term_sheets": [{"terms": t} for t in sheets]}
        if refs:
            msg["refs"] = refs[:20]
        return msg
