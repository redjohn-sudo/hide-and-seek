"""signals(case, data) -> dict: what the planner should know about the supplier, never sent as a claim.

Meeting notes, letters and e-mails are context, not checks (brief/api.md); our late payments are not citable
at all (no `inv:` in facts.json). The package's notes follow fixed wordings, so plain phrase matching is
reliable. Every signal lists the IDs it comes from, so the planner can quote the context in prose.
Documents come from the package (SQLite) and the Sunday release (`data/documents.csv`), up to the case's start:
a note later than the supplier's letter but known before the case is still ours to use.

  asked_pct, asked_price      the letter's demand (None without a price letter)
  weighted_index_pct          per part, the 12-month move of its cost drivers weighted by `cost_drivers`
  increase_justified          the weighted move meets the demand, within 1 point (P02): argue terms, not the price
  price_floor_hint            stabilise / secure_supply only: the unit price that minimises cost + relationship_risk
                              over a price grid (best package, our estimate of the supplier), capped at the asked price
                              (else the current price without a price letter). Below it, squeezing the supplier costs
                              more in relationship than it saves (P02: 366.87). None in other goals
                              None also when C cannot be computed at the current price, or when the minimum stays on
                              the low edge of the price grid even after widening it (no floor inside: not a floor)
  price_floor_confidence      "low" with the hint (None without): our supplier estimate is not calibrated on the arena
  price_floor_reason          why the confidence is low (P01 counter-example); without a hint, why there is none
                              ("cost: ..." or "edge: ...")
  suppliers[sup]              per supplier:
    committee_above_pct       price changes above this % need its pricing committee
    material_hedged_until     "Month YYYY" its material or energy is bought forward to (P08, P14)
    cash_need, free_capacity, full_order_book, open_book, reference_customer, small_account_review,
    not_strategic, expects_partnership, list_price_only, overdue_complaints   -> list of IDs
    credit_index_change       supplier_intel credit index now minus 12 months ago (higher = worse)
    single_source             no other active or qualified source for a part of the case
    our_late_payments         {invoices, late_share, avg_days_late} over the 12 months before the case

Never raises: a section that fails is logged and left at its empty value.
"""

import logging
import math
import re
from datetime import date, timedelta

from arena_kit.cost import RELATIONSHIP_GOALS, total_cost

from .economics import compile_case, estimate_supplier, relationship_risk
from .facts import _day, asked_price, case_start, facts_of, package, release_rows
from .index import asked_pct, driver_moves, increase_justified, weighted_move_pct

log = logging.getLogger(__name__)

NOTE_DAYS = 548  # notes of the last 18 months
FLOOR_STEPS = 400  # coarse price grid, then refined to the cent around its best point
FLOOR_LOWS = (0.8, 0.5, 0.2)  # low end of the grid, in current prices: widened while the minimum sits on it
NO_COST = "cost: C cannot be computed at the current price, so no floor"
ON_EDGE = "edge: the minimum stays on the low end of the price grid (down to 0.2 x current), so no floor inside it"
FLOOR_REASON = (
    "Not calibrated on the arena: the supplier's full cost is our should-cost estimate. Counter-example P01: the "
    "arena charged no relationship cost for the deal at 135.40, while this estimate charges 1,430 EUR (4,055 EUR "
    "if the supplier's standard payment is 30 days) and puts the floor at 137.42."
)
PAY_DAYS = 365

PHRASES = {
    "cash_need": ["pay earlier", "pay our invoices within", "spoke mostly about cash"],
    "free_capacity": ["could take additional parts at short notice"],
    "full_order_book": ["order intake is very strong", "very high capacity utilisation"],
    "open_book": ["walk us through their calculation"],
    "reference_customer": ["important reference customer"],
    "small_account_review": ["reviewing small accounts"],
    "not_strategic": ["'not strategic'"],
    "expects_partnership": ["expects partnership"],
    "list_price_only": ["are not discounted", "applies to all customers", "will not be extended"],
    "overdue_complaints": ["invoices are overdue"],
}
NEWS = {
    "free_capacity": "free_capacity",
    "new_plant": "free_capacity",
    "full_order_book": "full_order_book",
    "allocation": "full_order_book",
    "credit_downgrade": "cash_need",
    "credit_watch": "cash_need",
    "short_time_work": "cash_need",
}
COMMITTEE = re.compile(r"price changes above ([\d.]+) ?% need the approval")
HEDGED = re.compile(r"(?:forward|at fixed prices) until (\w+ \d{4})")


def _window(case: dict, facts: dict) -> tuple[date | None, date | None]:
    end = case_start(case, facts)
    return (end - timedelta(days=NOTE_DAYS) if end else None), end


def _inside(d: date | None, since: date | None, until: date | None) -> bool:
    return d is not None and not (since and d < since) and not (until and d > until)


def _documents(data, supplier: str, since: date | None, until: date | None) -> list[tuple[str, str]]:
    """(id, text) of the supplier's documents in the window: package, then Sunday release."""
    p = package(data)
    if p is None:
        return []
    rows = []
    if p.db is not None:
        rows = p.query("SELECT id, date FROM documents WHERE supplier_id = ? ORDER BY date", (supplier,), limit=1000)
    rows += [r for r in release_rows(data, "documents") if r.get("supplier_id") == supplier]
    out = []
    for r in rows:
        if not r.get("id") or not _inside(_day(r.get("date")), since, until):
            continue
        try:
            out.append((r["id"], " ".join(p.document(r["id"]).split())))
        except (FileNotFoundError, OSError, ValueError):
            log.warning("document %s unreadable, skipped", r["id"])
    return out


def _late_payments(data, supplier: str, until: date | None) -> dict:
    out = {"invoices": 0, "late_share": 0.0, "avg_days_late": 0.0}
    p = package(data)
    if p is None or p.db is None:
        return out
    end = until or date.max
    start = end - timedelta(days=PAY_DAYS) if until else date.min
    rows = p.query(
        "SELECT due_date, paid_date FROM invoices WHERE supplier_id = ? AND paid_date IS NOT NULL",
        (supplier,),
        limit=100_000,
    )
    days = ((_day(r["due_date"]), _day(r["paid_date"])) for r in rows)
    pairs = [(d, pd) for d, pd in days if d and pd and start <= d <= end]
    late = [(pd - d).days for d, pd in pairs if pd > d]
    if pairs:
        out["invoices"] = len(pairs)
        out["late_share"] = round(len(late) / len(pairs), 3)
        out["avg_days_late"] = round(sum(late) / len(late), 1) if late else 0.0
    return out


def _single_source(data, case: dict, supplier: str) -> bool | None:
    """True if a part of the case has no other active or qualified source; None if sourcing is unknown."""
    p = package(data)
    if p is None or p.db is None:
        return None
    for part in case["parts"]:
        rows = p.query("SELECT supplier_id, status FROM sourcing WHERE part_id = ?", (part,))
        if not rows:
            return None  # a part we know nothing about: no claim either way
        others = [r for r in rows if r["supplier_id"] != supplier and r["status"] in ("active", "qualified")]
        if not others:
            return True
    return False


def _from_documents(out: dict, docs: list[tuple[str, str]]) -> None:
    for doc_id, text in docs:
        low = text.lower()
        for name, phrases in PHRASES.items():
            if any(p in low for p in phrases):
                out[name].append(doc_id)
        if m := COMMITTEE.search(low):
            out["committee_above_pct"] = float(m.group(1))
            out["committee"].append(doc_id)
        if m := HEDGED.search(text):
            out["material_hedged_until"] = m.group(1)
            out["material_hedged"].append(doc_id)


def _from_news(out: dict, facts: dict, supplier: str, since: date | None, until: date | None) -> None:
    for nid, n in sorted(facts.items()):
        if not nid.startswith("news:") or not isinstance(n, dict) or n.get("supplier") != supplier:
            continue
        if n.get("topic") in NEWS and _inside(_day(n.get("date")), since, until):
            out[NEWS[n["topic"]]].append(nid)


def supplier_signals(case: dict, data, facts: dict, supplier: str) -> dict:
    since, until = _window(case, facts)
    out: dict = {name: [] for name in PHRASES}
    out |= {"committee": [], "material_hedged": [], "committee_above_pct": None, "material_hedged_until": None}
    out |= {"credit_index_change": None, "single_source": None, "our_late_payments": _late_payments(None, "", None)}
    steps = {
        "documents": lambda: _from_documents(out, _documents(data, supplier, since, until)),
        "news": lambda: _from_news(out, facts, supplier, since, until),
        "credit": lambda: out.update(credit_index_change=_credit_change(data, supplier)),
        "sourcing": lambda: out.update(single_source=_single_source(data, case, supplier)),
        "payments": lambda: out.update(our_late_payments=_late_payments(data, supplier, until)),
    }
    for name, step in steps.items():
        try:
            step()
        except Exception:
            log.exception("%s: signals %s for %s failed, left empty", case.get("case_id"), name, supplier)
    return out


def _credit_change(data, supplier: str) -> int | None:
    p = package(data)
    if p is None or p.db is None:
        return None
    rows = p.query("SELECT credit_index, credit_index_12m_ago FROM supplier_intel WHERE supplier_id = ?", (supplier,))
    if not rows or rows[0]["credit_index"] is None or rows[0]["credit_index_12m_ago"] is None:
        return None
    return rows[0]["credit_index"] - rows[0]["credit_index_12m_ago"]


def saturated(case: dict, data, facts: dict, supplier: str) -> bool:
    """The supplier says its order book is full or allocates (notes, letters, news): more volume is its leverage."""
    since, until = _window(case, facts)
    out = {name: [] for name in PHRASES} | {"committee": [], "material_hedged": []}
    _from_documents(out, _documents(data, supplier, since, until))
    _from_news(out, facts, supplier, since, until)
    return bool(out["full_order_book"])


def price_floor(case: dict, data) -> tuple[float | None, str | None]:
    """(floor, None), or (None, why not). The price that minimises cost + relationship_risk, stabilise /
    secure_supply only; never above the asked price, nor above the current price without a price letter."""
    b = case["buyer"]
    if b.get("category_goal") not in RELATIONSHIP_GOALS:
        return None, None
    current = b.get("current_unit_price") or b.get("fallback_unit_price")
    try:  # before compiling: a case whose C cannot be computed would log one error per term sheet tried
        computable = bool(current) and math.isfinite(total_cost(case, {"unit_price": current}))
    except Exception:
        computable = False
    if not computable:
        return None, NO_COST
    econ = compile_case(case, data=data)
    supplier = estimate_supplier(case, data=data)
    asked = asked_price(case, facts_of(case, data))
    cap = asked or current
    top = max(p for p in (current, asked, b.get("fallback_unit_price")) if p)

    def total(price: float) -> float:
        t = {**econ.best_package, "unit_price": round(price, 2)}
        return econ.cost(t) + relationship_risk(case, t, supplier=supplier)

    if not math.isfinite(total(current)):
        return None, NO_COST
    hi = 1.1 * top
    for low in FLOOR_LOWS:
        lo = low * current
        step = (hi - lo) / FLOOR_STEPS
        i = min(range(FLOOR_STEPS + 1), key=lambda k: total(lo + k * step))
        if i == 0:
            continue  # on the low edge: the minimum may lie below, widen the grid
        coarse = lo + i * step
        cents = range(max(round(lo * 100), round((coarse - step) * 100)), round((coarse + step) * 100) + 1)
        best = min((c / 100 for c in cents), key=total)
        return (min(best, cap), None) if math.isfinite(total(best)) else (None, NO_COST)
    return None, ON_EDGE


def price_floor_hint(case: dict, data) -> float | None:
    """The floor alone (see `price_floor`)."""
    return price_floor(case, data)[0]


def signals(case: dict, data) -> dict:
    out = {"asked_pct": None, "asked_price": None, "weighted_index_pct": {}, "increase_justified": None}
    out |= {"price_floor_hint": None, "price_floor_confidence": None, "price_floor_reason": None}
    out["suppliers"] = {}
    try:
        facts = facts_of(case, data)
        out["asked_pct"] = asked = asked_pct(case, facts)
        out["asked_price"] = asked_price(case, facts)
    except Exception:
        log.exception("%s: signals unavailable", case.get("case_id") if isinstance(case, dict) else None)
        return out
    try:
        p = package(data)
        for part in case["parts"] if p is not None and p.db is not None else []:
            moves = driver_moves(part, p, facts)
            if moves:
                out["weighted_index_pct"][part] = round(weighted_move_pct(moves), 2)
        weighted = out["weighted_index_pct"]
        if asked is not None and weighted:
            out["increase_justified"] = any(increase_justified(w, asked) for w in weighted.values())
    except Exception:
        log.exception("%s: index signals failed", case.get("case_id"))
    try:
        floor, why_not = price_floor(case, data)
        out["price_floor_hint"] = floor
        if floor is not None:
            out["price_floor_confidence"], out["price_floor_reason"] = "low", FLOOR_REASON
        else:
            out["price_floor_reason"] = why_not
    except Exception:
        log.exception("%s: price floor hint failed", case.get("case_id"))
    for s in case.get("suppliers") or []:
        out["suppliers"][s] = supplier_signals(case, data, facts, s)
    return out
