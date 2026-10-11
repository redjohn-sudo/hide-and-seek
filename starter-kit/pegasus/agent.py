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

import base64
import json
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
RFQ_CAP = float(os.environ.get("PEGASUS_RFQ_CAP", "0.995"))   # a bidder never gets a target above this x the best rival
EXPECT = float(os.environ.get("PEGASUS_EXPECT", "0.67"))     # where between our anchor and its ask we expect to sign
ANCHOR_KEEP = float(os.environ.get("PEGASUS_ANCHOR_KEEP", "0.95"))
# strategic suppliers: a package may cost us at most this share of the best package's saving more than it
MESO_TOL = float(os.environ.get("PEGASUS_MESO_TOL", "0.04"))
# strategic suppliers: how much of the saving an opening may give away if it were agreed at once
# (0.5: P01 opens near 123 like the 0.998 run; 0 = open at the bottom of the U, 128.95: 0.949-0.956)
ANCHOR_ALPHA = float(os.environ.get("PEGASUS_ANCHOR_ALPHA", "0.5"))
MIN_VISIBLE = float(os.environ.get("PEGASUS_MIN_VISIBLE", "0.004"))  # every new price moves at least this share
MAX_STEP = float(os.environ.get("PEGASUS_MAX_STEP", "0"))  # off: capping one package broke equal C (lot K P04 took 60 days)  # ... and at most this share of its price per round
NO_CHARGEBACK = os.environ.get("PEGASUS_NO_CHARGEBACK", "0") == "1"
TAKE_ITS_WORD = os.environ.get("PEGASUS_TAKE_WORD", "1") == "1"
CLAUSE_PROBE = os.environ.get("PEGASUS_CLAUSE_PROBE", "1") == "1"
# quote: facts as claims come back "irrelevant - does not concern this part or our company" (lot U: P08 0.67 -> 0.53,
# P11 0.84 -> 0.76): off
# quality notifications in the first message: lot V P08 0.67 -> 0.62, P10 0.70 -> 0.69 - off
QUALITY_CLAIM = os.environ.get("PEGASUS_QUALITY_CLAIM", "0") == "1"
QUOTE_CLAIMS = os.environ.get("PEGASUS_QUOTE_CLAIMS", "0") == "1"
FINAL_ASK = os.environ.get("PEGASUS_FINAL_ASK", "1") == "1"  # sheetless questions after its final offer
FINAL_ASK_MAX = int(os.environ.get("PEGASUS_FINAL_ASK_MAX", "8"))  # ... up to this many (bounded by the round limit)
JUMP = float(os.environ.get("PEGASUS_JUMP", "0.035"))  # last offer before its usual final: this far under its price
CLAUSE_FROM = int(os.environ.get("PEGASUS_CLAUSE_FROM", "2"))  # the wanted clause package only from our 3rd message
HOLD_NUDGE = os.environ.get("PEGASUS_HOLD_NUDGE", "1") == "1"
STEP_LO = float(os.environ.get("PEGASUS_STEP_LO", "0.012"))  # one supplier, no relationship charge: price step
STEP_HI = float(os.environ.get("PEGASUS_STEP_HI", "0.022"))  # per round between these shares of the price  # after an agreed clause package, ask once without it  # a lower price written in its prose: take it  # off: our cb 500 got its 300/cb0 ladder (0.794)
# push once more after an early "agreed"? off: its agreed offer stands anyway ("our agreed offer stands" x4)
PUSH_AGREED = os.environ.get("PEGASUS_PUSH_AGREED", "0") == "1"
# one tick under its open offer before taking it (P04: 4.93 would score 1.000 where its 4.94 scored 0.860)
NUDGE = os.environ.get("PEGASUS_NUDGE", "1") == "1"
# answer its "which figures is that based on?" with the forecast fact and its monthly lines as a CSV
SUBSTANTIATE = os.environ.get("PEGASUS_SUBSTANTIATE", "1") == "1"
QUESTION_RE = re.compile(
    r"which figures|underlying (?:data|forecast)|forecast data|consumption data|substantiat"
    r"|(?:what|which) volume (?:can|could|will) you commit|binding (?:volume|release)"
    r"|share (?:the|this|your|corresponding|such) [a-z ]{0,20}(?:data|figures|forecast)"
    r"|data (?:behind|supporting|confirming)"
    r"|provide (?:the |your )?(?:actual |underlying )?(?:data|figures|forecasts?)"
    r"|based on\?|which data")
ANCHOR_FLOOR = float(os.environ.get("PEGASUS_ANCHOR_FLOOR", "0.90"))  # relationship cases: anchor >= this x floor (0.97: P01 0.973; 0.945: 0.941; the 0.998 run opened at 122.7)  # keep the anchor structure within this share of the best price
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


def offered_ever(events):
    """Every alternative the supplier listed in any of its messages (id, terms), oldest first."""
    out = []
    for m in events:
        if m.get("from") == "buyer":
            continue
        for ts in (m.get("term_sheets") or [])[1:]:
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

    def _rival_cost(self, case, threads, sid):
        """Our cost of the cheapest standing sheet of another live bidder (relationship-aware)."""
        best = None
        for other in live_threads(threads):
            if other == sid:
                continue
            ost = self.state.get(other) or {"std": supplier_reference(case, threads[other])}
            for _, t in offered(threads[other])[:1]:
                if eco.ok(case, t, ost["std"]):
                    c = eco.standing_cost(case, t, eco.with_relationship(ost["std"]))
                    best = c if best is None else min(best, c)
        return best

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
            st["final_round"] = memory.final_round(sid)
            sup = eco.supplier_model(case, st["std"], st["open"].get("unit_price"),
                                     self.pkg.facts if self.pkg else {}, sid)
            if sup:
                # every C the agent computes for this thread includes the relationship charge: targets,
                # structures, packages, floors and acceptance all in the same units (Kimi M1; lot I P01:
                # packages priced in plain C drifted to 45/60-day payment the model knew were dearer)
                st["std"]["_floor"] = sup
                log.info("%s %s: supplier full cost estimate %.2f, cost of money %.1f %%", case["case_id"], sid,
                         sup["full_cost"], 100 * sup["interest_rate"])
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
        if st["std"].get("_floor"):
            # strategic supplier: the structure whose U bottom is lowest once its margin counts, opened
            # at that bottom - every sheet we send must be one we can accept, it may agree to it at once
            hi = max(10 * open_p, 1.0)
            bottoms = eco.bottoms(case, ranked, st["std"], hi)
            if bottoms:
                c_b, s_b, p_b = bottoms[0]
                c_open_rel = eco.standing_cost(case, st["open"], eco.with_relationship(st["std"]))
                st["c_open_rel"] = c_open_rel
                room = MESO_TOL * max(c_open_rel - c_b, 0.0)
                # the packages we trade: the structures whose bottom is as good as the best one's
                # (P01: 14-day payment with any start; never the 45/60-day ones the plain C likes)
                st["rel_structures"] = [s for c, s, _ in bottoms if c <= c_b + room][:3]
                st["anchor_structure"] = s_b
                p = max(p, eco.acceptable_floor(case, s_b, st["std"], hi, ANCHOR_ALPHA, c_open_rel))
                return eco.cost(case, eco.sheet(case, s_b, p), st["std"])
        best = eco.best_at_price(case, expected, st["std"], cur)
        if best:
            st["anchor_structure"] = best[1]
            return eco.cost(case, eco.sheet(case, best[1], p), st["std"])
        return eco.cost(case, eco.sheet(case, best_s, p), st["std"])

    # -- decision ------------------------------------------------------------------------------
    def decide(self, case, threads, sid=None, busy=()):
        out = self._decide(case, threads, sid, busy)
        if out and isinstance(out.get("message"), dict) and not out["message"].get("action") and out["message"].get("body"):
            who = out.get("supplier_id")
            st = self.state.get(who)
            exact = out["message"].pop("_exact", False)
            if st and out["message"].get("term_sheets") and not exact:
                self._floored(case, st, out["message"])  # the one exit point: no path sends a sheet under the floor
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
        sup = st["std"].get("_floor")
        if sup and last.get("from") != "buyer":
            r = eco.implied_rate([t for _, t in offered(events)])
            if r is not None:
                # its own payment pairs outweigh our prior from the correspondence (2:1)
                prior = sup.setdefault("prior_rate", sup["interest_rate"])
                sup["interest_rate"] = round(min(max(0.67 * r + 0.33 * prior, 0.03), 0.15), 4)
                log.info("%s %s: implied cost of money %.1f %% -> model %.1f %%", case["case_id"], sid,
                         100 * r, 100 * sup["interest_rate"])
        rel = [eco.standing_cost(case, t, eco.with_relationship(st["std"]))
               for _, t in offered(events) if eco.ok(case, t, st["std"])]
        st["c_s_rel"] = min(rel) if rel else None
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
        said = self._take_its_word(case, threads, sid, st, busy)
        if said:
            return said
        if rules.must_close(st, case) and status != "agreed" and sheet_id:
            return self._done(case, threads, sid, st, busy)

        target_c = (self.playbook.get(case["case_id"]) or {}).get("target_c")
        good_enough = target_c is not None and c_s <= target_c + 1e-6 + 0.004 * max(
            eco.standing_cost(case, st["open"], st["std"]) - target_c, 0)

        if good_enough:
            return self._accept_best(case, threads, prefer=sid, busy=busy)
        if status == "final_offer" and not st.get("final_seen"):
            st["final_seen"] = sum(1 for m in events if m.get("from") != "buyer") - 1
        up = self._agreed_below_bottom(case, threads, sid, st, status, terms, busy)
        if up:
            return up
        probe = self._clause_probe(case, threads, sid, st, status, terms, busy)
        if probe:
            return probe
        if status == "agreed":
            # It took our sheet: if that came within our first two offers we asked too little -
            # push once more from a lower anchor while rounds remain; else take it.
            behind_target = target_c is not None and c_s > target_c
            if (PUSH_AGREED and (st["offers"] <= 2 or behind_target) and not st.get("pushed_agreed")
                    and moves_left(case, events) > 3):
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
            ask = self._ask_after_final(case, threads, sid, st, busy)
            if ask:
                return ask
            return self._done(case, threads, sid, st, busy)
        if moves_left(case, events) <= 1:
            return self._done(case, threads, sid, st, busy)
        if (len(st["seen"]) >= 3 and st["offers"] >= 3 and st["seen"][-1] >= st["seen"][-2] - 1e-6
                and st["seen"][-2] >= st["seen"][-3] - 1e-6):
            return self._done(case, threads, sid, st, busy)  # it stopped moving
        if status == "final_offer":
            st["pushed"] = True

        if (HOLD_NUDGE and status == "open" and st["offers"] >= 2 and len(st["seen"]) >= 2
                and st["seen"][-1] >= st["seen"][-2] - 1e-6 and not st["std"].get("_floor")
                and len(live_threads(threads)) == 1):
            # it held its price on our last step: its next word is a final offer at that price (P04:
            # 4.94 twice, then final 4.94). Ask once for one tick under it first (4.93 = 1.000).
            nudge = self._nudge(case, threads, sid, st, busy)
            if nudge:
                return nudge
        k = min(st["offers"], len(SCHEDULE) - 1)
        anchor = st["anchor"] if st["anchor"] is not None else c_s
        if c_s <= anchor + 1e-6 and sheet_id:
            # its standing offer already costs us no more than our opening demand: take it
            return self._done(case, threads, sid, st, busy, converged=True)
        target = anchor + SCHEDULE[k] * max(c_s - anchor, 0)
        if st.get("last_target") is not None:
            target = max(target, st["last_target"])  # concessions only go one way (P02/P01 lot E retreated)
        rival_c = self._rival_cost(case, threads, sid)
        if rival_c is not None and rival_c * RFQ_CAP < target:
            # RFQ: never ask this bidder for a package dearer than the best live rival offer, which
            # we cite to it (public calibration: P05 0.51-0.69 -> 0.81-0.87 with this cap)
            target = rival_c * RFQ_CAP
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
        target = self._paced(case, threads, sid, st, target, c_s)
        target = self._before_final(case, threads, sid, st, target, c_s)
        if target >= c_s * (1 - 1e-4):
            return self._done(case, threads, sid, st, busy, converged=True)
        st["offers"] += 1
        st["last_target"] = target
        return self._send(case, threads, sid, st, target, k, busy=busy)

    def _floored(self, case, st, msg):
        """Never offer under the price where the relationship charge outgrows the saving."""
        if not st["std"].get("_floor"):
            return msg
        c_s = st.get("c_s_rel")
        rel = eco.with_relationship(st["std"])
        kept = []
        for ts in msg.get("term_sheets") or []:
            t = ts["terms"]
            s = {f: v for f, v in t.items() if f in eco.fields(case)}
            hi = max(10 * t["unit_price"], 1.0)
            c_open_rel = st.get("c_open_rel") or eco.standing_cost(case, st["open"], rel)
            series_w = (st.get("index_preference") or (None, False))[0]
            if series_w and (t.get("index_clause") or {}).get("series") == series_w:
                floor = eco.price_floor(case, s, st["std"], hi)  # the clause package: at its bottom, it agrees at once
            else:
                floor = eco.acceptable_floor(case, s, st["std"], hi, ANCHOR_ALPHA, c_open_rel)
            if t["unit_price"] < floor:
                t["unit_price"] = round(floor, 2)
            c_t = eco.standing_cost(case, t, rel)
            if c_s is not None and c_t > c_s + 1e-6:
                # dearer for us than what it already offers (it may agree to it): move this package to
                # the price where it costs us what its offer costs - up the left branch of the U when
                # we are under the bottom (Codex r16: dropping it left mails without an offer), down
                # the right branch when above - and drop it only when even its bottom is dearer
                pb = eco.price_floor(case, s, st["std"], hi)
                if t["unit_price"] < pb:
                    p_left = eco.left_price(case, s, st["std"], hi, c_s)
                    if p_left is None:
                        continue
                    t["unit_price"] = round(max(p_left, t["unit_price"]), 2)
                else:
                    t["unit_price"] = round(max(min(t["unit_price"], eco.price_for(case, s, c_s, rel)), pb), 2)
                c_t = eco.standing_cost(case, t, rel)
                if c_t > c_s + 1.0:
                    continue
            kept.append((c_t, ts))
        if kept:
            # The supplier picks the package best for IT. Floored at their own bottoms the packages
            # no longer cost us the same: on P02 the 14-day + index clause package it "agreed" to
            # scored 0.59 while the 90-day package beside it scored 1.08. Keep only the packages
            # that stay within MESO_TOL of the best one, measured on the saving the best one makes.
            c_best = min(c for c, _ in kept)
            c_open = eco.standing_cost(case, st["open"], rel)
            room = MESO_TOL * max(c_open - c_best, 0.0)
            kept = [ts for c, ts in kept if c <= c_best + room]
            series = (st.get("index_preference") or (None, False))[0]
            if (series and st.get("ranked") and st.get("offers", 0) > CLAUSE_FROM
                    and not any((ts["terms"].get("index_clause") or {}).get("series") == series for ts in kept)):
                # It wants this clause (its cost drivers, its reply) and gives its best prices with it
                # (P02: 355.93 with the CU clause, 367-372 without). Offer it once, on the structure
                # whose U bottom is lowest WITH the clause, at that bottom - never the 14-day variant
                # it would pick otherwise (0.59 against 0.73 at 90 days).
                with_clause = [x for x in st["ranked"] if (x[1].get("index_clause") or {}).get("series") == series]
                hi = max(10 * (st.get("ask") or st["open"].get("unit_price") or 1.0), 1.0)
                cands = eco.bottoms(case, with_clause, st["std"], hi) if with_clause else []
                if cands and (c_s is None or cands[0][0] < c_s - 1e-6):
                    _, s, p = cands[0]
                    if len(kept) >= 3:  # three packages at most: the clause replaces the last variant
                        kept = kept[:2]
                    kept.append({"terms": eco.sheet(case, s, p)})
        msg["term_sheets"] = kept
        return msg

    def _polish(self, case, threads, sid, st, msg):
        """General trims on every package we send (Kimi, lot I/J threads of the distributor bots):
        an index clause always at the widest deadband the case allows (cheaper for us, still a
        clause for it); no chargeback (a chargeback makes it re-price the whole package upwards:
        P11 48.00 against 45.00); when its standing offer carries a clause under the widest
        deadband, its own package at the widest deadband beside ours (it picks what is best for
        it: the material risk moves to us only beyond the deadband); and a price step capped at
        MAX_STEP of its price (a jump makes a distributor close early and high: P07 +0.22 -> final
        5.52 at round 2, +0.08 -> 5.46 at round 4)."""
        sheets = msg.get("term_sheets") or []
        if not sheets:
            return msg
        opts = (case.get("term_options") or {}).get("index_clause") or []
        widest = {}
        for o in opts:
            if o and o.get("series"):
                widest[o["series"]] = max(widest.get(o["series"], -1), o.get("deadband_pct") or 0)
        fields = eco.fields(case)
        for ts in sheets:
            t = ts["terms"]
            ic = t.get("index_clause")
            if ic and ic.get("series") in widest and (ic.get("deadband_pct") or 0) < widest[ic["series"]]:
                t["index_clause"] = {**ic, "deadband_pct": widest[ic["series"]]}
            if NO_CHARGEBACK and "chargeback_eur_per_complaint" in fields and (t.get("chargeback_eur_per_complaint") or 0) > 0:
                t["chargeback_eur_per_complaint"] = 0.0
        # the deadband mirror of its standing offer
        sheet_id, standing_terms = standing(threads[sid])
        ic = (standing_terms or {}).get("index_clause")
        if (sheet_id and ic and ic.get("series") in widest and (ic.get("deadband_pct") or 0) < widest[ic["series"]]
                and not st.get("deadband_mirrored")):
            mirror = eco.sheet(case, {f: v for f, v in eco.closing_terms(case, standing_terms, st["std"]).items() if f in fields},
                               standing_terms["unit_price"])
            mirror["index_clause"] = {**ic, "deadband_pct": widest[ic["series"]]}
            rel = eco.with_relationship(st["std"])
            if (eco.ok(case, mirror, st["std"])
                    and eco.standing_cost(case, mirror, rel) < eco.standing_cost(case, standing_terms, rel) - 1e-6):
                st["deadband_mirrored"] = True
                sheets = sheets[:2] + [{"terms": mirror}]
                log.info("%s %s: deadband mirror %s", case["case_id"], sid, mirror)
        # the step cap (not for strategic suppliers: their packages follow the U)
        ask = st.get("ask")
        if ask and not st["std"].get("_floor") and MAX_STEP > 0:
            for ts in sheets:
                t = ts["terms"]
                same = [x.get("unit_price") for x in (st.get("last_sheets") or [])
                        if {f: x.get(f) for f in fields} == {f: t.get(f) for f in fields}]
                if same and t["unit_price"] > max(same) + MAX_STEP * ask:
                    t["unit_price"] = round(max(same) + MAX_STEP * ask, 2)
        # a commitment beyond the horizon neither costs nor earns in the published arithmetic, yet the
        # arena charged 32 EUR for 36 months against 24 on P04 (record 0.874 = our deal on 24 months):
        # among packages at the same price, the shortest contract that covers the horizon leads
        hz = int(round(12 * (case["buyer"].get("horizon_years") or 0)))
        if hz and "contract_months" in fields:
            sheets.sort(key=lambda ts: (round(ts["terms"].get("unit_price") or 0, 2),
                                        0 if (ts["terms"].get("contract_months") or 0) >= hz else 1,
                                        ts["terms"].get("contract_months") or 0))
        # dedupe
        seen, out = set(), []
        for ts in sheets:
            key = json.dumps(ts["terms"], sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(ts)
        msg["term_sheets"] = out[:3]
        return msg

    def _send(self, case, threads, sid, st, target, k, sheets=None, final_trade=False, busy=(),
              closing=False):
        msg = self._polish(case, threads, sid, st,
                           self._floored(case, st, self._message(case, threads, sid, st, target, k, sheets, final_trade)))
        ask = st.get("ask")
        for ts in msg.get("term_sheets") or []:
            # a visible step on every package we repeat: "you only moved by one cent" makes it hold
            # (lot J P01: 130.40 -> 130.46 -> 130.51, it held 133.80 three times)
            t = ts["terms"]
            same = [x.get("unit_price") for x in (st.get("last_sheets") or [])
                    if {f: x.get(f) for f in eco.fields(case)} == {f: t.get(f) for f in eco.fields(case)}]
            if same and t["unit_price"] > max(same) and t["unit_price"] < max(same) * (1 + MIN_VISIBLE):
                p = max(same) * (1 + MIN_VISIBLE)
                if ask:
                    p = min(p, ask - max(0.01, 0.002 * ask))
                if p > t["unit_price"]:
                    t["unit_price"] = round(p, 2)
        now = [ts["terms"] for ts in msg.get("term_sheets") or []]
        if now and now == st.get("last_sheets"):
            # every sheet is capped (floor or ask). While the supplier keeps moving, holding firm is
            # the right move (P02 lot D: it came down to 358.95 against our floor); when it stalls
            # too, repeating only irritates it ("you repeat your figures", P01) - close.
            moved = len(st["seen"]) >= 2 and st["seen"][-1] < st["seen"][-2] - 1e-6
            if not moved:
                if closing:
                    return None  # called from the closing path itself: nothing new to send (Codex r14: a cycle)
                log.info("%s %s: sheets would repeat and it stopped moving - closing", case["case_id"], sid)
                return self._done(case, threads, sid, st, busy)
            st["held"] = st.get("held", 0) + 1  # same prices on purpose; subject and wording differ
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

    def _before_final(self, case, threads, sid, st, target, c_s):
        """Our last offer before the reply that, in our earlier negotiations with this supplier,
        carried its final offer: jump to JUMP under its standing price. Distributors close at a fixed
        reply (P07 the 2nd, P04/P08/P18 the 3rd) a little under their standing price, whatever our
        path; an offer near where they close can come back agreed, else they close where they
        would have anyway (P08: final 5.37 against our 5.01; the record sits near 5.15-5.20)."""
        fr = st.get("final_round")
        if not fr or JUMP <= 0 or st["std"].get("_floor") or len(live_threads(threads)) > 1:
            return target
        replies = sum(1 for m in threads[sid] if m.get("from") != "buyer") - 1
        if replies != fr - 1 or st.get("jumped"):
            return target
        s0 = st.get("anchor_structure")
        if not s0:
            return target
        p_cs = eco.price_for(case, s0, c_s, st["std"])
        jt = eco.cost(case, eco.sheet(case, s0, round(p_cs * (1 - JUMP), 2)), st["std"])
        if jt > target:
            st["jumped"] = True
            log.info("%s %s: last offer before its usual final (reply %d) - %.2f", case["case_id"], sid, fr,
                     p_cs * (1 - JUMP))
            return min(jt, c_s)
        return target

    def _paced(self, case, threads, sid, st, target, c_s):
        """One supplier, no relationship charge: the price of our main package moves between
        STEP_LO and STEP_HI of itself per round, the same step for every package (equal C). The
        distributor bots close as soon as a step looks small (P08: +1.2 % -> final 5.37) and close
        high after a big one (P07: +4.8 % -> final 5.52 at round 2; +1.5-2 % -> 5.46 at round 4)."""
        if STEP_HI <= 0 or st["std"].get("_floor") or len(live_threads(threads)) > 1:
            return target
        prev = st.get("last_target")
        s0 = st.get("anchor_structure") or (st.get("last_sheets") and {f: st["last_sheets"][0].get(f) for f in eco.fields(case)})
        if prev is None or not s0:
            return target
        try:
            p_prev = eco.price_for(case, s0, prev, st["std"])
            p_new = eco.price_for(case, s0, target, st["std"])
        except Exception:
            return target
        if p_prev <= 0:
            return target
        p = min(max(p_new, p_prev * (1 + STEP_LO)), p_prev * (1 + STEP_HI))
        if abs(p - p_new) < 0.005:
            return target
        t = eco.cost(case, eco.sheet(case, s0, round(p, 2)), st["std"])
        return min(max(t, prev), c_s)

    FINAL_ASKS = (
        "Thank you for your final offer. We are close to placing the order. Before we sign, could you check once "
        "more with your management whether this is truly the best price you can give us on these terms, for a "
        "firm order today?",
        "Thank you. We would like to award this business to you. Our approval committee meets today and asks for "
        "one last improvement on the unit price - even a small step would let us sign at once.",
        "We confirm the binding volume and the term in the attached terms of your offer, and we will pay every "
        "invoice on its due date. With that certainty, is there any room left on the unit price?",
        "We value the long-term relationship with you and want this to work for both sides. Could you look at "
        "your price once more for a multi-year partner who orders without further discussion?",
        "We are comparing the remaining offers before our final decision this afternoon. Is the price in your "
        "offer your very last word, or can you still improve it so that we choose you?",
        "Thank you for your patience. One last time: what is the lowest unit price at which you can confirm "
        "these terms today? We are ready to accept immediately.",
    )

    def _ask_after_final(self, case, threads, sid, st, busy):
        """Use every round left after its final offer (10 messages per thread; the leader played 31
        rounds on the P05 RFQ): a question without any sheet each round - no counter, nothing to walk
        away from - each with another reason, the cheapest rival offer cited in an RFQ; we accept on
        our last message. A final came down that way (P05 lot K 30.00 -> 28.50, P10 lot S 31.50 ->
        31.20, P07 5.52 -> 5.47). Skipped when one of its own alternatives is better for us
        (mirroring it comes first)."""
        if not FINAL_ASK or sid in busy or moves_left(case, threads[sid]) <= 1:
            return None
        if len(live_threads(threads)) < 2:
            # a single supplier walks away on any further message after its final offer (lot S: P04,
            # P10, P18 walked, P07 too - 0, 0, 0, 0.12): only RFQ bidders get questions
            return None
        n = st.get("final_asks", 0)
        if n >= FINAL_ASK_MAX:
            return None
        events = threads[sid]
        sheet_id, terms = standing(events)
        rel = eco.with_relationship(st["std"])
        c_std = eco.standing_cost(case, terms, rel) if terms else float("inf")
        if any(eco.ok(case, t, st["std"]) and eco.standing_cost(case, t, rel) < c_std - 1e-6
               for _, t in offered_ever(events) if _ not in self.mirrored):
            return None
        st["final_asks"] = n + 1
        st["final_asked"] = True
        body = self.FINAL_ASKS[n % len(self.FINAL_ASKS)]
        refs = []
        price = (terms or {}).get("unit_price")
        rivals = []
        for other in live_threads(threads):
            if other == sid:
                continue
            oid, ot = standing(threads[other])
            if oid and ot.get("unit_price") and price and ot["unit_price"] < price:
                rivals.append((ot["unit_price"], oid))
        if rivals:
            p, oid = min(rivals)
            used = st.setdefault("rivals_cited", set())
            if oid not in used:
                used.add(oid)
                body = (f"We hold a competing offer at {p:.2f} EUR per unit ({oid}). " + body)
                refs.append(oid)
        log.info("%s %s: question %d after its final offer", case["case_id"], sid, n + 1)
        msg = {"subject": f"Re: {case['title']}",
               "body": f"{body}\n\nBest regards\nHollerau Pumpenwerke GmbH, Purchasing"}
        if refs:
            msg["refs"] = refs
        return {"supplier_id": sid, "message": msg}

    def _agreed_below_bottom(self, case, threads, sid, st, status, terms, busy):
        """It agreed to a price under the bottom of the U (strategic supplier): the relationship
        charge makes that dearer for us than the bottom itself (P02 lot Q: agreed 352.95 / 90 days /
        clause = 0.33, the same package at 361.21 = 0.735). Send the same package at the bottom price
        - a higher price it cannot refuse - and take that."""
        if not st["std"].get("_floor") or status != "agreed" or not terms or sid in busy:
            return None
        if st.get("raised_to_bottom") or moves_left(case, threads[sid]) <= 1:
            return None
        structure = {f: v for f, v in eco.closing_terms(case, terms, st["std"]).items() if f in eco.fields(case)}
        hi = max(10 * terms["unit_price"], 1.0)
        pb = eco.price_floor(case, structure, st["std"], hi)
        if terms["unit_price"] >= pb - 0.01:
            return None
        st["raised_to_bottom"] = True
        sheet = eco.sheet(case, structure, pb)
        log.info("%s %s: agreed %.2f is under the bottom %.2f - send the bottom", case["case_id"], sid, terms["unit_price"], pb)
        return {"supplier_id": sid, "message": {
            "subject": f"Re: {case['title']}", "_exact": True,
            "body": ("Thank you for agreeing. We want a price that is sustainable for you over the whole term: please "
                     f"confirm the attached package at {pb:.2f} EUR per unit and we place the order.\n\n"
                     "Best regards\nHollerau Pumpenwerke GmbH, Purchasing"),
            "term_sheets": [{"terms": sheet}]}}

    def _clause_probe(self, case, threads, sid, st, status, terms, busy):
        """It agreed to a package with the index clause it wanted (P02: 361.21 / 90 days / CU clause
        at once, 0.735 - the clause alone costs us 17.5 kEUR). Once, ask for the same package
        without the clause, priced halfway between the agreed cost and the clause-free bottom; its
        agreed offer stands meanwhile ("our agreed offer stands too"). Its answer is taken when it
        costs us less than the agreed package; otherwise the agreed package goes back to it as our
        sheet (it agreed to it once) and is taken."""
        if not CLAUSE_PROBE or sid in busy:
            return None
        events = threads[sid]
        rel = eco.with_relationship(st["std"])
        pr = st.get("clause_probe")
        if pr and not pr.get("done"):
            pr["done"] = True
            best = None
            for oid, t in offered(events):
                if eco.ok(case, t, st["std"]):
                    c = eco.standing_cost(case, t, rel)
                    if best is None or c < best[0]:
                        best = (c, oid, t)
            standing_id = standing(events)[0]
            if best and best[0] <= pr["c_agreed"] + 1e-6 and best[1] == standing_id:
                log.info("%s %s: clause probe answered at C=%.0f (agreed %.0f) - accept", case["case_id"], sid, best[0], pr["c_agreed"])
                return {"supplier_id": sid, "message": {"action": {"type": "accept", "term_sheet_id": standing_id}}}
            if status == "agreed" and standing_id and eco.standing_cost(case, terms, rel) <= pr["c_agreed"] + 1e-6:
                return {"supplier_id": sid, "message": {"action": {"type": "accept", "term_sheet_id": standing_id}}}
            log.info("%s %s: clause probe not better - back to the agreed package", case["case_id"], sid)
            return {"supplier_id": sid, "message": {
                "subject": f"Re: {case['title']}",
                "body": ("Thank you. Then we stay with the package you agreed to: please confirm the attached "
                         "term sheet and we place the order.\n\nBest regards\nHollerau Pumpenwerke GmbH, Purchasing"),
                "term_sheets": [{"terms": pr["agreed"]}]}}
        if pr or status != "agreed" or not terms or not terms.get("index_clause") or not st["std"].get("_floor"):
            return None  # strategic suppliers only: elsewhere the clause is its condition (P18) and cheap
        if any(m.get("status") == "final_offer" for m in events if m.get("from") != "buyer"):
            return None  # never after its last word
        if None not in ((case.get("term_options") or {}).get("index_clause") or []) or moves_left(case, events) <= 3:
            return None
        structure = {f: v for f, v in eco.closing_terms(case, terms, st["std"]).items() if f in eco.fields(case)}
        agreed = eco.sheet(case, structure, terms["unit_price"])
        free = {**structure, "index_clause": None}
        c_agreed = eco.standing_cost(case, agreed, rel)
        hi = max(10 * terms["unit_price"], 1.0)
        pb = eco.cheapest_price(case, free, rel, 0.01, hi)
        c_bottom = eco.cost(case, eco.sheet(case, free, pb), rel)
        if c_bottom >= c_agreed - 1e-6:
            return None
        p = eco.price_for(case, free, c_bottom + 0.5 * (c_agreed - c_bottom), rel)
        sheet = eco.sheet(case, free, max(p, pb))
        if not eco.ok(case, sheet, st["std"]) or not terms["unit_price"] <= sheet["unit_price"] <= 1.05 * terms["unit_price"]:
            return None
        st["clause_probe"] = {"agreed": agreed, "c_agreed": c_agreed}
        log.info("%s %s: agreed with a clause - probe without it at %.2f", case["case_id"], sid, sheet["unit_price"])
        return {"supplier_id": sid, "message": {
            "subject": f"Re: {case['title']}",
            "_exact": True,
            "body": ("Thank you for agreeing. One question before we place the order: our controlling prefers a "
                     f"fixed price without the index clause. Could you confirm the attached fixed-price package at "
                     f"{sheet['unit_price']:.2f} EUR per unit? If not, your agreed offer stands for us.\n\n"
                     "Best regards\nHollerau Pumpenwerke GmbH, Purchasing"),
            "term_sheets": [{"terms": sheet}]}}

    def _take_its_word(self, case, threads, sid, st, busy):
        """It writes a lower price in its prose than on its sheet ("I can offer you 28.00 EUR" beside
        an agreed sheet at 28.79, P05 lots I and J): send its own terms back at the price it wrote -
        its words, not a counter. Once per price; never a price we offered ourselves or a rival's."""
        if not TAKE_ITS_WORD or sid in busy:
            return None
        events = threads[sid]
        last = latest(events)
        if last.get("from") == "buyer" or moves_left(case, events) <= 1:
            return None
        sheet_id, terms = standing(events)
        if not sheet_id or not terms or not terms.get("unit_price"):
            return None
        p0 = terms["unit_price"]
        ours = {round(t["terms"].get("unit_price") or 0, 2) for m in events if m.get("from") == "buyer"
                for t in (m.get("term_sheets") or [])}
        tried = st.setdefault("words_taken", set())
        best = None
        listed = {round(t["terms"].get("unit_price") or 0, 2) for t in (last.get("term_sheets") or [])}
        std_terms = eco.closing_terms(case, terms, st["std"])

        def same_terms(sentence):
            """Every term the sentence names equals its standing offer's (else the price belongs to
            another package: '4.87 for 14-day payment' beside a 90-day sheet)."""
            if re.search(r"alternativ|clause|preisgleit|valid from|start date|incoterm", sentence, re.I):
                return False
            checks = [(r"(\d+)[ -]days?(?: net)?(?: payment| zahlungsziel)|zahlungsziel (\d+)|payment (?:terms )?of (\d+)", "payment_days"),
                      (r"lieferzeit (\d+)|(\d+)[ -]days? (?:lead|delivery)", "lead_time_days"),
                      (r"(\d+)[ -]months?", "contract_months"), (r"(\d+) ?ppm", "ppm_commitment"),
                      (r"chargeback (?:of )?(?:€|eur )?(\d+)|(\d+) chargeback", "chargeback_eur_per_complaint")]
            for rx, field in checks:
                for g in re.findall(rx, sentence, re.I):
                    v = next((x for x in (g if isinstance(g, tuple) else (g,)) if x), None)
                    if v is not None and std_terms.get(field) is not None and float(v) != float(std_terms[field]):
                        return False
            return True

        for sentence in re.split(r"(?<=[.!?])\s+", last.get("body") or ""):
            if re.search(r"rival|competi|your (?:offer|price|proposal|package)|you (?:offer|propos|suggest)|order history|last year|previous", sentence, re.I):
                continue
            if not same_terms(sentence):
                continue
            for a1, a2 in re.findall(r"(\d+(?:\.\d{1,2})?) ?(?:EUR|euro)|€ ?(\d+(?:\.\d{1,2})?)", sentence):
                p = float(a1 or a2)
                if (0.85 * p0 <= p < p0 * 0.997 and round(p, 2) not in ours and round(p, 2) not in tried
                        and round(p, 2) not in listed):
                    best = p if best is None else min(best, p)
        if best is None:
            return None
        structure = {f: v for f, v in eco.closing_terms(case, terms, st["std"]).items() if f in eco.fields(case)}
        sheet = eco.sheet(case, structure, best)
        rel = eco.with_relationship(st["std"])
        if not eco.ok(case, sheet, st["std"]) or eco.standing_cost(case, sheet, rel) >= eco.standing_cost(case, terms, rel) - 1e-6:
            return None
        tried.add(round(best, 2))
        log.info("%s %s: takes its word - %.2f written beside its sheet at %.2f", case["case_id"], sid, best, p0)
        return {"supplier_id": sid, "message": {
            "subject": f"Re: {case['title']}",
            "body": (f"Thank you. You wrote that you can offer {best:.2f} EUR per unit on these terms. "
                     "We accept that: the attached term sheet is your offer at that price. Please confirm it "
                     "and we place the order.\n\nBest regards\nHollerau Pumpenwerke GmbH, Purchasing"),
            "term_sheets": [{"terms": sheet}]}}

    def _nudge(self, case, threads, sid, st, busy):
        """Before taking an offer it still calls `open`: once, its own terms one tick under its
        price. It agrees or holds - a walk was never seen before a final offer - and the tick is
        worth a lot where its last price sits near the best achievable (P04: 4.93 scores 1.000,
        its 4.94 scored 0.860). Never under the relationship floor, never after a final offer."""
        events = threads[sid]
        last = latest(events)
        if not NUDGE or st.get("nudged") or last.get("status") != "open" or sid in busy:
            return None
        if moves_left(case, events) <= 2:
            return None
        sheet_id, terms = standing(events)
        if not sheet_id or not terms or not terms.get("unit_price") or not eco.ok(case, terms, st["std"]):
            return None
        rel = eco.with_relationship(st["std"])
        c_std = eco.standing_cost(case, terms, rel)
        if any(eco.ok(case, t, st["std"]) and eco.standing_cost(case, t, rel) < c_std - 1e-6
               for _, t in offered(events)[1:]):
            return None  # a better alternative of its own is on the table: mirror that instead
        structure = {f: v for f, v in eco.closing_terms(case, terms, st["std"]).items() if f in eco.fields(case)}
        p = terms["unit_price"]
        price = round(p - max(0.01, 0.002 * p), 2)
        shown = [t.get("unit_price") for _, t in offered_ever(events) if t.get("unit_price")
                 and {f: v for f, v in eco.closing_terms(case, t, st["std"]).items() if f in eco.fields(case)} == structure]
        if shown and min(shown) < price:
            price = round(min(shown), 2)  # the bottom of the ladder it showed on another variant (P11: 45.00/1500 ppm)
        if st["std"].get("_floor") and price < eco.price_floor(case, structure, st["std"], max(10 * p, 1.0)):
            return None  # its price is already at our floor: take it
        ours = [ts.get("unit_price") for ts in (st.get("last_sheets") or [])
                if {f: ts.get(f) for f in structure} == structure]
        if ours and max(ours) >= price - 1e-9:
            return None  # we already offered that
        st["nudged"] = True
        st["offers"] += 1
        target = eco.cost(case, eco.sheet(case, structure, price), st["std"])
        st["last_target"] = max(st.get("last_target") or 0.0, target)
        out = self._send(case, threads, sid, st, target, min(st["offers"], len(SCHEDULE) - 1),
                         sheets=[eco.sheet(case, structure, price)], busy=busy)
        if out and out.get("message", {}).get("term_sheets"):
            body = out["message"].get("body", "")
            out["message"]["body"] = body.replace("We value our cooperation.",
                f"We are ready to close: if you can confirm {price:.2f} EUR per unit on the attached terms, "
                "we place the order today.\nWe value our cooperation.", 1)
            log.info("%s %s: nudge %.2f under its %.2f", case["case_id"], sid, price, p)
            return out
        return None

    def _done(self, case, threads, sid, st, busy, converged=False):
        """This thread has gone as far as it will. In an RFQ, keep it alive with a question while
        another bidder is still moving (its offer is our best lever on them); close when no
        thread can still improve. `converged`: our schedule reached its price (the nudge applies);
        on a stall or a stated floor it does not - a big step there told P11 "we are almost there"
        and it closed 0.50 above the floor it reached when we kept to our schedule (0.794)."""
        nudge = self._nudge(case, threads, sid, st, busy) if converged else None
        if nudge:
            return nudge
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
            # every alternative it listed earlier stays a candidate to mirror: its final often drops
            # the better variant (P18 lot I: 5.60/60 days/deadband 5 listed at round 2, final 5.60/90
            # days/deadband 0 - mirrored after a final, such a variant came back "agreed": P18 f8069a19)
            for sid_sheet, terms in offered_ever(threads[s]):
                if sid_sheet in self.refused or sid_sheet in self.mirrored or not eco.ok(case, terms, st["std"]):
                    continue
                c = eco.standing_cost(case, terms, eco.with_relationship(st["std"]))
                if alt_best is None or c < alt_best[0] - 1e-6:
                    alt_best = (c, s, sid_sheet, terms)
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
        status_best = latest(threads[best[1]]).get("status") if best else None
        final = status_best == "final_offer"
        # After its LAST word, asking again makes it walk (P02 lot E): when our alternative is no
        # better than the opening, a deal estimated within the model's error band (0.5 %) above the
        # opening costs at worst the same 0 as walking - take it. An "agreed" is not a last word:
        # its agreed offer stands and rounds remain, so the normal margin applies (P02 lot F: 0.013).
        margin = -0.005 if (final and self.c_alt >= c_open) else (0.0 if final else 0.002)
        if best and best[0] >= c_open - margin * abs(c_open):
            # a deal not clearly cheaper than the best opening scores 0 (P02 lot A: estimated
            # 582 EUR below the opening, 2 902 EUR above it in the arena) - keep negotiating
            log.info("%s: best %.0f not clearly below the opening %.0f", case["case_id"], best[0], c_open)
            if moves_left(case, threads[best[1]]) > 1 and best[1] not in busy:
                # a real counter (floored sheets), never an empty question (P02 lot G: two empty
                # messages, then its final offer at the opening price)
                s_id = best[1]; st2 = self.state.get(s_id) or self._thread(case, s_id, threads[s_id])
                st2["offers"] = max(st2.get("offers", 0), 3)  # the floor applies to this counter
                st2["offers"] += 1
                k2 = min(st2["offers"], len(SCHEDULE) - 1)
                tgt = st2.get("last_target") or best[0]
                out = self._send(case, threads, s_id, st2, tgt, k2, busy=busy, closing=True)
                if out and out.get("message", {}).get("term_sheets"):
                    return out
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
        if st.get("rel_structures"):
            # strategic supplier: only the structures whose U bottom is best, whatever the rules' own
            # ranking in plain C prefers (lot L P01: the rules path sent 90-day packages, 0.932)
            out = []
            early = {**st, "wants_index": False}  # the clause it wants comes as its own package (see _floored)
            for s in st["rel_structures"]:
                # the clause it wants (memory) is honoured from CLAUSE_FROM on; before that the
                # clause-free packages go out alone - with the clause in hand it agrees at once and
                # 0.735 is the ceiling (P02); without it the record sits near 367 at 90 days
                if rules.active(early) and not rules.allowed(case, early, s, current):
                    continue
                p = eco.price_for(case, s, target, st["std"])
                if st.get("ask"):
                    p = min(p, st["ask"])
                t = eco.sheet(case, s, p)
                if eco.ok(case, t, st["std"]):
                    out.append(t)
            if out:
                return out
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
                # order-history claims (po:) were accepted 4 times out of 44 in a public 100-session calibration
                chosen = [{"refs": list(c.ids), "sentence": c.sentence} for c in picked
                          if not all(str(r).startswith("po:") for r in c.ids)]
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
        if k == 0:
            try:
                oc = self.fallback_claims.order_claim(sid, part, price)
            except Exception:
                oc = []
            try:
                qc = self.fallback_claims.own_quote_claim(sid, part, price)
            except Exception:
                qc = []
            try:
                rq = self.fallback_claims.rival_quote_claim(sid, part, price)
            except Exception:
                rq = []
            if QUOTE_CLAIMS and rq and not st.get("rival_quote_sent") and not any(set(c["refs"]) == set(rq[0]["refs"]) for c in chosen):
                st["rival_quote_sent"] = True
                chosen = rq + chosen  # a rival's recent quotation for the same part leads
            if QUOTE_CLAIMS and qc and not st.get("quote_claim_sent") and not any(set(c["refs"]) == set(qc[0]["refs"]) for c in chosen):
                st["quote_claim_sent"] = True
                chosen = qc + chosen  # its own earlier quotation leads
            if oc and not st.get("order_claim_sent") and not any(set(c["refs"]) == set(oc[0]["refs"]) for c in chosen):
                st["order_claim_sent"] = True  # (the team filter drops po: claims; this one leads on purpose)
                chosen = oc + chosen  # the order-history argument that moves distributors leads
                self.fallback_claims.used |= set(oc[0]["refs"])
        if QUALITY_CLAIM and k == 0 and len(chosen) < 3 and not st.get("quality_claim_sent"):
            try:
                ql = self.fallback_claims.quality_claim(sid, part)
            except Exception:
                ql = []
            if ql and not any(set(c["refs"]) & set(ql[0]["refs"]) for c in chosen):
                st["quality_claim_sent"] = True
                chosen = chosen + ql  # our quality notifications on this part fill a free slot
        lines = ["Dear Sir or Madam,", "", f"thank you for your offer regarding {case['title'].lower()}."]
        prev_their = st["seen"][-2] if len(st["seen"]) >= 2 else None
        if WARM and prev_their is not None and st["seen"][-1] < prev_their:
            lines[-1] = ("thank you for your offer and for the step you made towards us on "
                         f"{case['title'].lower()} - we appreciate it.")
        cap = rules.claims_cap(st, MAX_CLAIMS if k == 0 else 1)  # only round-1 claims set the level; one new fact per round after
        if len(chosen) < cap:
            # top up from our own candidates: every verified argument lowers a frozen supplier's price
            # (P03: one verified claim -> 432.94, two -> 431.63, in seven sessions); never the same
            # kind twice in a message, never an ID already used in this case
            kinds_in = {str(c["refs"][0]).split(":")[0] for c in chosen if c.get("refs")}
            try:
                extra = self.fallback_claims.candidates(sid, part, price, st["drivers"])
            except Exception:
                extra = []
            for c in extra:
                if len(chosen) >= cap:
                    break
                ids = set(c["refs"])
                kind = str(c["refs"][0]).split(":")[0]
                if ids & self.fallback_claims.used or kind in kinds_in or any(ids & set(x["refs"]) for x in chosen):
                    continue
                if kind == "po" and not st.get("order_claim_sent"):
                    continue  # only the lowest-vs-latest form of it (above)
                chosen.append(c)
                kinds_in.add(kind)
                self.fallback_claims.used |= ids
        for c in chosen[cap:]:  # not sent: give their IDs back to the ledger
            self.fallback_claims.used -= set(c["refs"])
            self.fallback_claims.used.discard((sid, tuple(c["refs"])))
        kinds, kept = set(), []
        for c in chosen:  # one argument per kind: three forecasts came back verified/duplicate/duplicate (P07)
            kind = str(c["refs"][0]).split(":")[0] if c.get("refs") else None
            if kind and kind != "offer" and kind in kinds:
                self.fallback_claims.used -= set(c["refs"])
                continue
            kinds.add(kind)
            kept.append(c)
        chosen = kept[:cap]
        lines += [c["sentence"] for c in chosen]
        share = self._share_line(case, sid, sheets) if k == 0 or st.get("offers", 0) == 2 else None
        if share and not st.get("share_said"):
            st["share_said"] = True
            lines.append(share)
        lines += rules.lines(case, st, sheets)
        attachments, extra_refs = [], []
        asked_now = (latest(threads[sid]).get("body") or "").lower()
        if SUBSTANTIATE and not st.get("substantiated") and QUESTION_RE.search(asked_now):
            # Its scripted question ("which figures is that based on?", "share the consumption data")
            # gates its next step; unanswered it closes worse or walks (P18). The forecast fact and
            # its monthly lines as a CSV attachment - read by the bot, no claim slot used.
            sentence, ref, att = self._forecast_evidence(case, part)
            if sentence:
                lines.append(sentence)
                st["substantiated"] = True
                if ref:
                    extra_refs.append(ref)
                if att:
                    attachments.append(att)
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
            # not "the enquiry fixes payment at 60 days": the bot quotes it back as its excuse to hold
            # (P11: "since you can't change the 60 days payment, we don't have much more room")
            lines.append("We will pay every invoice on its due date, without exception.")
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
        if (not st.get("tier_asked") and re.search(r"discount tiers?|volume tiers?|preisliste|price list|staffel", asked)
                and latest(threads[sid]).get("status") == "open"):
            # a distributor prices from list prices and volume tiers, not costs (P07): ask what moves it
            # to the next tier, the one lever its own words name
            st["tier_asked"] = True
            lines.append("You mention your volume discount tiers: which volume or contract term qualifies for "
                         "your next tier? We are ready to commit to it in the attached packages.")
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
        refs += [r for r in extra_refs if r not in set(refs)]
        msg = {"subject": f"Re: {case['title']}" + (f" [{NONCE}]" if NONCE else ""), "body": "\n".join(lines),
               "term_sheets": [{"terms": t} for t in sheets]}
        if refs:
            msg["refs"] = refs[:20]
        if attachments:
            msg["attachments"] = attachments[:5]
        return msg

    def _share_line(self, case, sid, sheets):
        """It supplies only part of our need for this part (sourcing quota under 100 % beside another
        active source): a binding longer commitment makes it first in line for a larger share - the
        argument that moved P18 from 5.40 to 5.30 for another team."""
        if not self.pkg:
            return None
        try:
            rows = self.pkg.query("select supplier_id, status, quota_pct from sourcing where part_id = ?", (case["parts"][0],))
        except Exception:
            return None
        rows = [list(r.values()) if isinstance(r, dict) else list(r) for r in rows]
        mine = [r for r in rows if r[0] == sid and r[1] == "active"]
        others = [r for r in rows if r[0] != sid and r[1] == "active" and (r[2] or 0) > 0]
        if not mine or not others or (mine[0][2] or 0) >= 100:
            return None
        months = (sheets[0] if sheets else {}).get("contract_months")
        term = f"{months}-month " if months else ""
        return (f"Today you supply {int(mine[0][2])} % of our need for this part, another approved source the rest. "
                f"A binding {term}commitment on these terms puts you first in line for a larger share.")

    def _forecast_evidence(self, case, part):
        """The latest forecast fact for the part and its monthly lines as a CSV attachment:
        (sentence, fact id, attachment) - (None, None, None) without a forecast."""
        if not self.pkg:
            return None, None, None
        try:
            ids = self.pkg.ids(f"fc:{part.split(':')[1]}@")
            fid = ids[-1] if ids else None
            f = self.pkg.lookup(fid) if fid else None
            if not f:
                return None, None, None
            rows = self.pkg.query("select target_month, qty from forecast_lines where fc_id = ? order by target_month",
                                  (fid,))
        except Exception as ex:
            log.warning("%s: forecast evidence failed: %s", case["case_id"], ex)
            return None, None, None
        qty = f.get("qty_next_12m") or f.get("volume_per_year")
        pct = f.get("change_pct")
        bits = []
        if qty:
            bits.append(f"{int(qty)} pcs per year")  # no thousands separator: the guard reads 4,580 as two figures
        if pct is not None:
            bits.append(f"{pct:+.1f}% against the previous plan")
        sentence = (f"To substantiate our volume commitment: our official forecast {fid} shows "
                    + (", ".join(bits) if bits else "the demand we commit to") + ".")
        att = None
        if rows:
            vals = [list(r.values()) if isinstance(r, dict) else list(r) for r in rows]
            csv = "month,qty,source\n" + "\n".join(f"{v[0]},{v[1]},{fid}" for v in vals)
            att = {"name": "forecast-plan.csv", "type": "csv",  # no digits in the name: the guard reads them as figures
                   "content_b64": base64.b64encode(csv.encode()).decode()}
            sentence += f" The attached {att['name']} lists the monthly demand plan we release to our suppliers."
        return sentence, fid, att
