"""Adapter for Pegasus (`hollerau/pegasus/`, Vincent's agent): our claims and planner hints, without touching its code.

pegasus_claims(case, package, pegasus_claims_list, used=None, supplier=None, used_by_supplier=None) -> list[Claim]
    The union of our claims (`compile_evidence`) and Pegasus candidates (`Claims.candidates` / `rival_claim` dicts:
    `refs`, `sentence`, optional `rival`), each ID once, strongest first. Our filters apply to the Pegasus ones:
    no should-cost unless clearly below the price at stake, no volume growth in a capacity case or to a saturated
    supplier, scorecards only past our thresholds, no index claim when the increase is justified, and every claim
    must pass `check()` - except a rival's session offer (`offer:`), which the arena checks itself. At most 3 per
    message: the caller slices.
    Memory, as the arena counts (api.md): a rival's `offer:` or `quote:` counts once per supplier, every other ID
    once per case. `used`: IDs already sent in this case (global); it may also hold Pegasus `pick` entries
    `(supplier, refs)` for rival offers. `used_by_supplier`: {supplier: rival IDs already sent to it}. `supplier`:
    who this message goes to - then no card, news, letter or contract about another supplier, and no rival offer or
    quote already sent to it or its own. Only a candidate whose refs are all `offer:` strings skips `check()`; one
    whose refs are not a list of strings is rejected. `remember()` records a sent message.
planner_hints(case, data, standard=None) -> dict
    What the planner needs in one place: levers in EUR, best package, price floor and its confidence, and the
    signals that change a move (pricing committee, cash need, material covered, increase justified).

Never raises (`remember` included): on any failure, our own claims (or empty hints), logged.
"""

import logging
import re

from hollerau.contracts import Claim

from .compile import compile_evidence
from .economics import compile_case
from .facts import check, comparable, facts_of, has, price_letter, stake_price
from .forecast_cost_news import CAPACITY_TYPES, COST_ERROR, FC_GROWTH_PCT, NEWS_TOPICS
from .index import asked_pct, driver_moves, increase_justified, weighted_move_pct
from .scorecard import dimension_ok
from .signals import saturated, signals

log = logging.getLogger(__name__)

RANK = {"notice": 0, "quote": 1, "scorecard": 2, "index": 3, "orders": 4, "forecast_cost_news": 5}
FAMILY = {
    "idx": "index",
    "doc": "notice",
    "ctr": "notice",
    "cost": "forecast_cost_news",
    "fc": "forecast_cost_news",
    "sc": "scorecard",
    "quote": "quote",
    "offer": "quote",
    "po": "orders",
    "news": "forecast_cost_news",
}
FIGURES = [  # (family, ID prefix, regex on a Pegasus sentence, unit)
    ("index", "idx", r"changed (-?[\d.]+)%", "%"),
    ("notice", "doc", r"gives (\d+) days", "days"),
    ("forecast_cost_news", "cost", r"is ([\d.]+) EUR", "EUR"),
    ("forecast_cost_news", "fc", r"up ([\d.]+)%", "%"),
    ("scorecard", "sc", r"delivery was ([\d.]+)%", "%"),
    ("scorecard", "sc", r"quality was ([\d.]+) ppm", "ppm"),
    ("quote", "offer", r"offer at ([\d.]+) EUR", "EUR"),
]
SUPPORTS = {"index": "price", "notice": "effective_date", "quote": "price", "orders": "price"}


def as_claim(candidate: dict) -> Claim:
    """A Pegasus candidate as a `Claim`: family from its first ID, figure and unit read from its sentence."""
    refs, text = list(candidate["refs"]), candidate["sentence"]
    prefix = refs[0].split(":", 1)[0]
    family = FAMILY.get(prefix, prefix)
    supports = {"sc": "other", "fc": "volume"}.get(prefix, SUPPORTS.get(family, "price"))
    for fam, pre, pattern, unit in FIGURES:
        if prefix == pre and (m := re.search(pattern, text)):
            return Claim(fam, refs, float(m.group(1)), unit, text, "quality" if unit == "ppm" else supports)
    return Claim(family, refs, None, "", text, supports)


def _justified(case: dict, data, facts: dict) -> bool:
    """The supplier's increase is explained by its cost drivers (within a point): index claims argue its case."""
    asked = asked_pct(case, facts)
    if asked is None:
        return False
    return any(
        increase_justified(weighted_move_pct(moves), asked)
        for part in case["parts"]
        if (moves := driver_moves(part, data, facts))
    )


def _keep(case: dict, data, facts: dict, claim: Claim, justified: bool) -> bool:
    """Our filters, applied to one Pegasus claim (not a rival's offer)."""
    if not check(claim, facts):
        return False
    prefix = claim.ids[0].split(":", 1)[0]
    row = facts.get(claim.ids[0])
    if prefix == "cost":
        stake = stake_price(case, facts)
        return (
            has(row, "unit_cost")
            and stake is not None
            and comparable(case, data, row)
            and row["unit_cost"] * (1 + COST_ERROR) < stake
        )
    if prefix == "fc":
        if case.get("type") in CAPACITY_TYPES or any(saturated(case, data, facts, s) for s in case["suppliers"]):
            return False
        return has(row, "change_pct") and row["change_pct"] >= FC_GROWTH_PCT
    if prefix == "sc":
        return dimension_ok(row, claim.unit == "ppm")
    if prefix == "idx":
        return not justified
    if prefix == "news":
        return has(row, "topic") and row["topic"] in NEWS_TOPICS
    if prefix == "doc" and claim.family == "forecast_cost_news":
        return not price_letter(case, facts)
    return prefix in {"doc", "ctr", "quote", "po"}


PER_SUPPLIER = ("offer:", "quote:")  # a rival's offer or quote counts once per supplier (api.md)
ABOUT_SUPPLIER = ("sc:", "news:", "doc:", "ctr:")  # facts about one supplier: only to that supplier


def _supplier_no(supplier) -> str:
    return str(supplier).split(":", 1)[-1]


def _ids_of(claim) -> list[str]:
    """The IDs of a sent claim (`Claim` or Pegasus dict); [] if they are not all strings."""
    ids = claim.ids if isinstance(claim, Claim) else claim.get("refs") if isinstance(claim, dict) else None
    return list(ids) if isinstance(ids, list | tuple) and all(isinstance(i, str) for i in ids) else []


def _offers_sent(supplier, used: set, used_by_supplier: dict) -> set[str]:
    """Rival IDs already cited to `supplier`: `used_by_supplier`, plus Pegasus `pick` entries `(supplier, refs)`."""
    sent = used_by_supplier.get(supplier) if supplier is not None else None
    out = {i for i in sent if isinstance(i, str)} if isinstance(sent, set | frozenset | list | tuple) else set()
    for entry in used:
        if isinstance(entry, tuple) and len(entry) == 2 and entry[0] == supplier and isinstance(entry[1], tuple):
            out |= {r for r in entry[1] if isinstance(r, str)}
    return out


def remember(claims, supplier, used: set, used_by_supplier: dict | None = None) -> None:
    """Record a sent message: rival IDs per supplier (in `used_by_supplier`, and in `used` as Pegasus `pick` does,
    `(supplier, refs)`), every other ID in `used`. Never raises: a malformed claim or memory is skipped, logged."""
    try:
        for claim in claims or ():
            ids = _ids_of(claim)
            rivals = [i for i in ids if i.startswith(PER_SUPPLIER)]
            if rivals:
                used.add((supplier, tuple(ids)))
                if isinstance(used_by_supplier, dict):
                    used_by_supplier.setdefault(supplier, set()).update(rivals)
            used.update(i for i in ids if not i.startswith(PER_SUPPLIER))
    except Exception:
        log.exception("remember: memory not updated for %r", supplier)


def _rival_offer(candidate: dict) -> bool:
    """A rival's session offer: every ref an `offer:` string. Only these skip `check()` (the arena checks them)."""
    refs = candidate.get("refs")
    return bool(refs) and all(isinstance(r, str) and r.startswith("offer:") for r in refs)


def _well_formed(candidate) -> bool:
    refs = candidate.get("refs") if isinstance(candidate, dict) else None
    return (
        isinstance(refs, list | tuple)
        and bool(refs)
        and all(isinstance(r, str) for r in refs)
        and isinstance(candidate.get("sentence"), str)
    )


def _for_recipient(claim: Claim, supplier, facts: dict) -> bool:
    """Whether the claim may go to `supplier`: never another supplier's card, news, letter or contract; never a
    rival's offer or quote back to its own supplier."""
    for i in claim.ids:
        owner = (facts.get(i) or {}).get("supplier") if isinstance(facts.get(i), dict) else None
        if i.startswith(ABOUT_SUPPLIER) and owner is not None and owner != supplier:
            return False
        if i.startswith("quote:") and owner == supplier:
            return False
        if i.startswith(f"offer:{_supplier_no(supplier)}-"):
            return False
    return True


def pegasus_claims(
    case: dict, package, pegasus_claims_list, used=None, supplier=None, used_by_supplier=None
) -> list[Claim]:
    try:
        return _pegasus_claims(case, package, pegasus_claims_list, used, supplier, used_by_supplier)
    except Exception:
        log.exception("pegasus_claims failed: our claims only")
        try:
            return compile_evidence(case, package) if isinstance(case, dict) else []
        except Exception:
            return []


def _pegasus_claims(case, package, candidates, used, supplier, used_by_supplier) -> list[Claim]:
    case_id = case.get("case_id") if isinstance(case, dict) else None
    used = (
        {u for u in used if isinstance(u, str | tuple)} if isinstance(used, set | frozenset | list | tuple) else set()
    )
    by_supplier = used_by_supplier if isinstance(used_by_supplier, dict) else {}
    sent = _offers_sent(supplier, used, by_supplier)
    try:
        ours = compile_evidence(case, package)
    except Exception:
        log.exception("%s: our claims failed", case_id)
        ours = []
    rivals, pending = [], []
    for candidate in candidates if isinstance(candidates, list | tuple) else ():
        if not _well_formed(candidate):
            log.warning("%s: malformed Pegasus candidate rejected: %r", case_id, candidate)
        elif _rival_offer(candidate):
            rivals.append(as_claim(candidate))  # admitted before any filter
        else:
            pending.append(candidate)
    try:
        facts = facts_of(case, package)
    except Exception:
        log.exception("%s: facts unavailable, Pegasus claims other than rival offers dropped", case_id)
        facts, pending = {}, []
    try:
        justified = _justified(case, package, facts)
    except Exception:
        log.exception("%s: index check failed, no Pegasus index claim", case_id)
        justified = True
    theirs = []
    for candidate in pending:
        try:
            claim = as_claim(candidate)
            if _keep(case, package, facts, claim, justified):
                theirs.append(claim)
        except Exception:
            log.warning("%s: Pegasus candidate skipped: %r", case_id, candidate)
    out, seen = [], {u for u in used if isinstance(u, str)}
    for claim in sorted(ours + rivals + theirs, key=lambda c: RANK.get(c.family, len(RANK))):  # stable: ours first
        ids = set(claim.ids)
        rival_ids = {i for i in ids if i.startswith(PER_SUPPLIER)}
        if supplier is not None and (rival_ids & sent or not _for_recipient(claim, supplier, facts)):
            continue  # already sent to this supplier, its own, or about another supplier
        if seen & ids:
            continue  # an ID once per message; non-rival IDs once per case (`used`)
        seen |= ids
        out.append(claim)
    return out


def planner_hints(case: dict, data, standard=None) -> dict:
    out = {
        "reference_price": None,
        "best_package": {},
        "levers": [],
        "c_alt": None,
        "price_floor_hint": None,
        "price_floor_confidence": None,
        "price_floor_reason": None,
        "asked_pct": None,
        "increase_justified": None,
        "suppliers": {},
    }
    try:
        econ = compile_case(case, standard, data)
        out["reference_price"], out["best_package"], out["c_alt"] = econ.reference_price, econ.best_package, econ.c_alt
        out["levers"] = [
            {"field": lv.field, "best": lv.best, "worst": lv.worst, "eur": lv.cost_of_forgetting_eur}
            for lv in econ.levers
        ]
    except Exception:
        log.exception("%s: economics hints failed", case.get("case_id") if isinstance(case, dict) else None)
    try:
        s = signals(case, data)
        for key in (
            "price_floor_hint",
            "price_floor_confidence",
            "price_floor_reason",
            "asked_pct",
            "increase_justified",
        ):
            out[key] = s.get(key)
        for sup, sig in (s.get("suppliers") or {}).items():
            out["suppliers"][sup] = {
                "committee_above_pct": sig.get("committee_above_pct"),
                "cash_need": bool(sig.get("cash_need")),
                "material_hedged_until": sig.get("material_hedged_until"),
                "full_order_book": bool(sig.get("full_order_book")),
                "free_capacity": bool(sig.get("free_capacity")),
                "single_source": sig.get("single_source"),
                "sources": {k: sig.get(k) for k in ("committee", "cash_need", "material_hedged") if sig.get(k)},
            }
    except Exception:
        log.exception("%s: signal hints failed", case.get("case_id") if isinstance(case, dict) else None)
    return out
