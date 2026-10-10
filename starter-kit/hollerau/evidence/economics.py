"""compile_case(case, standard, data) -> Economics: what each term sheet costs us, and which levers matter most.

All arithmetic is the kit's (`arena_kit.cost`). `cost` is the C the arena charges: no supplier estimate,
hence no relationship term (decision D1); `relationship_risk` exposes that estimate apart, as a warning.
Allowed values come from `term_options` only (decision D2). Levers are valued at the price the supplier's letter
asks for, else the current price (decision D3). Recompile once the quote's standard terms are read.

Never raises: a step that fails is logged and leaves its part empty (no levers, an empty package, c_alt inf).
"""

import itertools
import logging
import math

from arena_kit import cost as kit
from hollerau.contracts import Economics, Lever, Terms

from .facts import asked_price, comparable, facts_of, has

log = logging.getLogger(__name__)

MAX_GRID = 5000  # beyond this many combinations, coordinate descent instead of exhaustive search


def _current_price(case: dict) -> float:
    b = case.get("buyer") or {}
    return b.get("current_unit_price") or b.get("fallback_unit_price") or math.nan


def lever_price(case: dict, data=None) -> float:
    """The price levers are valued at (D3): the letter's `asked_price`, else the current price. At the current
    price a later effective date is worth nothing, while the supplier asks for more."""
    try:
        return asked_price(case, facts_of(case, data)) or _current_price(case)
    except Exception:
        log.exception("%s: lever price", case.get("case_id"))
        return _current_price(case)


def _c_alt(case: dict) -> float:
    alt = case["buyer"].get("alternative") or {}
    if alt.get("kind", "none") == "none" or not alt.get("terms"):
        return math.inf
    return kit.total_cost(case, alt["terms"])  # inf without a price


def _grid(case: dict) -> dict[str, list]:
    """Allowed values of each negotiable non-price field; a field without options keeps the buyer's plan."""
    plan = kit.closing_terms(case, {})
    grid = {}
    for field in case["negotiable"]:
        if field == "unit_price":
            continue
        options = case.get("term_options", {}).get(field)
        if not options:
            log.warning("%s: negotiable field %s has no term_options, kept at the buyer's plan", case["case_id"], field)
            grid[field] = [plan.get(field)]
        else:
            grid[field] = list(options)
    return grid


def _grid_must_haves(case: dict, grid: dict, standard: Terms | None) -> list[dict]:
    """The must-haves the grid search can test: not the price (the planner's call), not a field the term sheet does
    not carry and nobody has filled yet (a field only the supplier's quote fills, before round 0)."""
    known = kit.closing_terms(case, {}, standard)
    kept = []
    for m in case["buyer"].get("must_haves", []):
        f = m.get("field")
        if f == "unit_price" or (f not in grid and known.get(f) is None):
            log.warning("%s: must-have on %s left out of the term search", case["case_id"], f)
            continue
        kept.append(m)
    return kept


def _search(grid: dict[str, list], c, ok, case_id: str) -> Terms:
    """The must-have-compatible combination of least C (first one on ties). If none meets them, the cheapest one,
    with a warning: the planner must not send it as compatible."""
    fields = list(grid)
    product = itertools.islice(itertools.product(*grid.values()), MAX_GRID)
    combos = [dict(zip(fields, combo, strict=True)) for combo in product]
    valid = [t for t in combos if ok(t)]
    if not valid:
        log.warning("%s: no combination of term_options meets the must-haves; cheapest one returned", case_id)
        valid = combos
    if math.prod(len(v) for v in grid.values()) <= MAX_GRID:
        return min(valid, key=c)
    best, improved = valid[0], True
    while improved:
        improved = False
        for f in fields:
            for v in grid[f]:
                t = {**best, f: v}
                if ok(t) and c(t) < c(best):
                    best, improved = t, True
    return best


def compile_case(case: dict, standard: Terms | None = None, data=None) -> Economics:
    """`standard`: the supplier's standard terms from its quote PDF (`arena_kit.docs.standard_terms`).
    Unknown before round 0: the planner recompiles once the quote is read. `data`: the package with the Sunday
    release; pass it explicitly in the final (default: the package on disk). `reference_price` is the lever price."""
    case_id = case.get("case_id", "?") if isinstance(case, dict) else "?"
    ref = lever_price(case, data)

    def cost(terms: Terms) -> float:
        try:
            return kit.total_cost(case, terms, standard)
        except Exception:
            log.exception("%s: cost", case_id)
            return math.inf

    def must_haves_ok(terms: Terms) -> bool:
        try:
            return kit.must_haves_ok(case, terms, standard)
        except Exception:
            log.exception("%s: must-haves", case_id)
            return False

    try:
        c_alt = _c_alt(case)
    except Exception:
        log.exception("%s: c_alt", case_id)
        c_alt = math.inf

    best, levers = {}, []
    try:
        grid = _grid(case)
        search_case = {**case, "buyer": {**case["buyer"], "must_haves": _grid_must_haves(case, grid, standard)}}

        def c(t: Terms) -> float:
            return cost({**t, "unit_price": ref})

        def ok(t: Terms) -> bool:
            return kit.must_haves_ok(search_case, {**t, "unit_price": ref}, standard)

        best = _search(grid, c, ok, case_id)
        for field, options in grid.items():
            if not case.get("term_options", {}).get(field):
                continue
            valid = [v for v in options if ok({**best, field: v})] or [best[field]]
            worst = max(valid, key=lambda v, f=field: c({**best, f: v}))
            levers.append(Lever(field, best[field], worst, round(c({**best, field: worst}) - c(best), 2)))
        levers.sort(key=lambda lv: lv.cost_of_forgetting_eur, reverse=True)
    except Exception:
        log.exception("%s: term search failed, no levers", case_id)
        best, levers = {}, []
    return Economics(case_id, cost, must_haves_ok, levers, best, c_alt, ref)


def estimate_supplier(case: dict, data=None, standard: Terms | None = None) -> dict:
    """Our estimate of the supplier for `arena_kit.cost.supplier_value`: full cost from the should-cost
    (`cost:<part>`, when comparable with the case price: one part, same price unit), else the current price less
    the case's sustainable margin (it is assumed at break-even, marked `estimated`)."""
    b = case["buyer"]
    full, estimated = None, True
    facts = facts_of(case, data)
    for part in case.get("parts", [])[:1]:
        row = facts.get("cost:" + part.split(":", 1)[1])
        if has(row, "unit_cost") and comparable(case, data, row):
            full, estimated = row["unit_cost"], False
    if full is None:
        full = _current_price(case) * (1 - (b.get("sustainable_margin_pct") or 0) / 100)
    std = {k: v for k, v in kit.closing_terms(case, {}, standard).items() if k != "unit_price"}
    return {"full_cost": full, "estimated": estimated, "interest_rate": b["interest_rate"], "standard": std}


def relationship_risk(case: dict, terms: Terms, standard: Terms | None = None, supplier=None, data=None) -> float:
    """EUR the deal would cost in relationship (stabilise / secure_supply) on our estimate of the supplier.
    A warning for the planner: the arena's C (`Economics.cost`) does not include it. 0 if it cannot be estimated."""
    try:
        supplier = supplier or estimate_supplier(case, data, standard)
        return kit.relationship_cost(case, terms, standard, supplier)
    except Exception:
        log.exception("%s: relationship risk", case.get("case_id"))
        return 0.0
