"""Numbers for the negotiation, all from the published cost function - never from the model.

`grid` enumerates every allowed combination of the non-price negotiable fields; `price_for` finds
the unit price that makes a structure cost a given C; `structures` ranks structures by what they
cost us at the price that leaves the supplier indifferent (its cash value unchanged), which is how
we find trades that are cheap for it and valuable for us.
"""

import os
import re
from itertools import combinations, product
from statistics import median
from pathlib import Path

from arena_kit.cost import closing_terms, complete_offer, must_haves_ok, supplier_value, total_cost

MAX_GRID = 2000


def fields(case):
    """Negotiable fields other than the price."""
    return [f for f in case["negotiable"] if f != "unit_price"]


def grid(case):
    """Every allowed combination of the non-price negotiable fields (one plan value when a field
    has no options)."""
    plan = complete_offer(case, {"unit_price": 0.0})
    choices = []
    for f in fields(case):
        opts = case.get("term_options", {}).get(f)
        choices.append(list(opts) if opts else [plan[f]])
    out = []
    for combo in product(*choices):
        out.append(dict(zip(fields(case), combo)))
        if len(out) >= MAX_GRID:
            break
    return out


def sheet(case, structure, price):
    """A complete, valid term sheet."""
    return complete_offer(case, {**structure, "unit_price": round(price, 2)})


RELATIONSHIP_GOALS = {"stabilise", "secure_supply"}
# full cost = the package's should-cost / 1.02: fitted on the arena's own relationship charges in our
# ended deals, 0.9804 / 0.9804 / 0.9805 of the should-cost on P01 / P02 / P03 (charges reproduced
# within 4-13 EUR once the supplier's cost of money is right)
SHOULD_COST_RATIO = 1 / 1.02
# the supplier's cost of money, fitted the same way: 9.6 % for the one whose correspondence asks to
# be paid faster (P01: "Could you pay our invoices within 10 days? In return we would grant a cash
# discount"), 6.6 % and 6.1 % for the two that never did (P02, P03); the buyer's 8 % fits none
CASH_RATE, CALM_RATE = 0.096, 0.064
CASH_WORDS = re.compile(r"pay (?:our invoices )?(?:earlier|within \d+ days)|cash discount|liquidity|short of cash"
                        r"|bank is watching|skonto", re.I)


def cash_strapped(sid, facts, root=None):
    """Does the correspondence show that this supplier's money is dear - an e-mail asking to be paid
    within N days against a cash discount, a call note headed Liquidity ("the bank is watching every
    month")? Read from the data package on disk (`ARENA_DATA`, plus the Sunday release)."""
    roots = [Path(root or os.environ.get("ARENA_DATA", "data/package"))]
    roots.append(roots[0] / "release-sunday")
    for k, v in (facts or {}).items():
        if not k.startswith("doc:") or not isinstance(v, dict) or v.get("supplier") != sid:
            continue
        text = v.get("title") or ""
        for r in roots:
            p = r / "correspondence" / (k[4:] + ".md")
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                break
        if CASH_WORDS.search(text):
            return True
    return False


def supplier_model(case, std, opening_price, facts, sid=None):
    """Our estimate of the supplier for the relationship term (strategic and bottleneck goals):
    full cost = the part's should-cost / 1.02 when the package has one, cost of money from the
    correspondence (see `cash_strapped`). None otherwise, and for other goals (the term does not
    apply)."""
    b = case["buyer"]
    if b.get("category_goal") not in RELATIONSHIP_GOALS or not b.get("relationship_eur_per_point"):
        return None
    fc = None
    for part in case.get("parts", []):
        f = (facts or {}).get(f"cost:{part.split(':')[1]}") or {}
        if f.get("unit_cost") and f.get("price_unit", 1) == 1:
            fc = f["unit_cost"] * SHOULD_COST_RATIO
            break
    if not fc:
        # no should-cost: no estimate (an opening-price guess broke the sandbox D01: 0.979 -> 0)
        return None
    sid = sid or (case.get("suppliers") or [None])[0]
    rate = CASH_RATE if sid and cash_strapped(sid, facts) else CALM_RATE
    return {"standard": {k: v for k, v in std.items() if k != "_supplier"},
            "interest_rate": rate, "full_cost": fc}


def cost(case, terms, std=None):
    """C with the kit's cost function; if `std` carries our supplier estimate ("_supplier"), the
    relationship term is priced too - a price below the supplier's cost is not a bargain (P02:
    344.20 EUR cost 143 193 EUR of relationship charge, the deal scored 0)."""
    std = std or {}
    sup = std.get("_supplier")
    clean_std = {k: v for k, v in std.items() if not k.startswith("_")}
    clean = {k: v for k, v in terms.items() if not k.startswith("_")}
    return total_cost(case, clean, clean_std, sup)


def with_relationship(std):
    """`std` with the supplier estimate switched on for C (acceptance decisions), if we have one.
    Offers are built without it - it only floors their prices (see `price_floor`)."""
    if std and std.get("_floor"):
        return {**std, "_supplier": std["_floor"]}
    return std


def price_floor(case, structure, std, hi):
    """The price under which the relationship charge outgrows the saving (bottom of the U)."""
    if not std or not std.get("_floor"):
        return 0.01
    return cheapest_price(case, structure, with_relationship(std), 0.01, hi)


def implied_rate(sheets):
    """The supplier's own cost of money, from two sheets of the SAME message that differ only in
    payment days and price (its standing offer beside "Alternatively, with Zahlungsziel 14 days
    net: ..."): value parity p1 (1 - r d1 / 360) = p2 (1 - r d2 / 360). P02: 373.88 at 90 days
    beside 368.68 at 14 -> 6.6 %, the rate fitted on the arena's own charges. None without a pair."""
    rates = []
    for x, y in combinations(sheets, 2):
        diff = {k for k in set(x) | set(y) if x.get(k) != y.get(k)}
        if diff != {"unit_price", "payment_days"}:
            continue
        p1, d1, p2, d2 = x["unit_price"], x["payment_days"], y["unit_price"], y["payment_days"]
        den = p1 * d1 - p2 * d2
        if not den:
            continue
        r = 360 * (p1 - p2) / den
        if 0.02 <= r <= 0.20:
            rates.append(r)
    return median(rates) if rates else None


def bottoms(case, ranked, std, hi):
    """Every ranked structure at the bottom of its U, cheapest first: [(C with the relationship
    charge, structure, bottom price)]. Which package is best for us once the supplier's margin
    counts is not which is best at its price: on P01 the cash-strapped foundry's charge outgrows
    the interest 90-day payment saves, so 14-day payment at 130.65 (score 1.000 if accepted)
    beats 90 days at 133.41 (0.916)."""
    rel = with_relationship(std)
    out = []
    for _, s, _ in ranked:
        p = cheapest_price(case, s, rel, 0.01, hi)
        out.append((cost(case, sheet(case, s, p), rel), s, p))
    out.sort(key=lambda x: x[0])
    return out


def ok(case, terms, std=None):
    return must_haves_ok(case, terms, std or {})


def alt_cost(case):
    """C of the alternative (no deal), inf when there is none."""
    alt = case["buyer"].get("alternative") or {}
    if alt.get("kind") in (None, "none") or not (alt.get("terms") or {}).get("unit_price"):
        return float("inf")
    return total_cost(case, alt["terms"])


def cheapest_price(case, structure, std, lo, hi):
    """With a relationship charge C(P) is U-shaped: the price where it bottoms out."""
    for _ in range(60):
        a, b = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        if cost(case, sheet(case, structure, a), std) <= cost(case, sheet(case, structure, b), std):
            hi = b
        else:
            lo = a
    return round((lo + hi) / 2, 2)


def price_for(case, structure, target_c, std=None, lo=0.01, hi=None):
    """Unit price at which `structure` costs `target_c` (C rises with the price above the bottom
    of the U; a target below the bottom is unreachable and gets the bottom price)."""
    hi = hi or max(10 * (case["buyer"].get("fallback_unit_price") or 1), 1.0)
    if (std or {}).get("_supplier"):
        lo = max(lo, cheapest_price(case, structure, std, lo, hi))
    for _ in range(40):
        mid = (lo + hi) / 2
        if cost(case, sheet(case, structure, mid), std) > target_c:
            hi = mid
        else:
            lo = mid
    return round(lo, 2)


def supplier_estimate(std, rate=0.08):
    return {"standard": std, "interest_rate": rate}


def indifferent_price(case, structure, ref_terms, std, rate=0.08):
    """The price for `structure` that is worth the same cash to the supplier as `ref_terms`."""
    sup = supplier_estimate(std, rate)
    want = supplier_value(case, ref_terms, sup)
    lo, hi = 0.01, max(10 * ref_terms["unit_price"], 1.0)
    for _ in range(60):
        mid = (lo + hi) / 2
        if supplier_value(case, sheet(case, structure, mid), sup) > want:
            hi = mid
        else:
            lo = mid
    return round(lo, 2)


def structures(case, ref_terms, std, rate=0.08, indifferent=False):
    """Structures that keep the must-haves, cheapest for us first: [(C, structure, price)].
    Priced at the supplier's own price (the replays show suppliers accept 90-day payment or a
    later effective date without charging their full cash value), or with `indifferent` at the
    price that keeps the supplier's cash value unchanged."""
    out = []
    for s in grid(case):
        p = indifferent_price(case, s, ref_terms, std, rate) if indifferent else ref_terms["unit_price"]
        t = sheet(case, s, p)
        if ok(case, t, std):
            out.append((cost(case, t, std), s, p))
    # Ties (within 0.05 % of C) go to the structure closest to the supplier's own terms: a change
    # it does not need is a change it may charge for.
    if not out:
        return out
    step = max(min(x[0] for x in out) * 0.0005, 1.0)

    def key(x):
        moved = sum(1 for f, v in x[1].items() if f in ref_terms and v != ref_terms[f])
        return (round(x[0] / step), moved, x[0])

    return sorted(out, key=key)


def standing_cost(case, terms, std):
    """C of a supplier's standing term sheet (its standard terms fill the gaps)."""
    if not terms or terms.get("unit_price") is None:
        return float("inf")
    return cost(case, closing_terms(case, terms, std), std)


def distance(case, structure, current):
    """Claude's tie-break: distance to current terms, scaled by each option range."""
    if not current:
        return 0.0
    d = 0.0
    for f, v in structure.items():
        cur = current.get(f)
        if cur is None or v == cur:
            continue
        opts = [o for o in case.get("term_options", {}).get(f, []) if isinstance(o, (int, float))]
        span = max(opts) - min(opts) if opts else 0
        if isinstance(v, (int, float)) and isinstance(cur, (int, float)) and span:
            d += abs(v - cur) / span
        else:
            d += 1.0
    return d


def priced_structures(case, target_c, std, current=None, ask=None, index_series=None,
                      require_index=False):
    """Legal offers within C, including concessions across several fields.

    A supplier's stated requirement is applied only to an allowed, structured index family.
    No response, score, private reservation price or cost-breakdown floor enters this grid.
    """
    rows = []
    for s in grid(case):
        clause = s.get("index_clause")
        months = (current or {}).get("contract_months")
        long_term = not months or s.get("contract_months", months) >= months
        if require_index and long_term and (not clause or clause.get("series") != index_series):
            continue
        p = price_for(case, s, target_c, std)
        if ask:
            p = min(p, ask)
        t = sheet(case, s, p)
        c = cost(case, t, std)
        if p > 0.01 and ok(case, t, std) and c <= target_c + 0.01:
            rows.append((p, c, s))
    return sorted(rows, key=lambda r: (-r[0], distance(case, r[2], current), r[1]))


def best_for_target(case, target_c, std, current=None, ask=None, limit=3,
                    adapt=False, index_series=None, require_index=False):
    """Structures that leave the supplier the highest price at our target C - the cheapest way
    for us to give it what it values most (price), whatever the supplier's own price is.
    Returns [(price, structure)], best first; alternatives vary a lever the others do not."""
    if adapt:
        candidates = priced_structures(case, target_c, std, current, ask, index_series, require_index)
        if not candidates:
            return []
        main = candidates[0]
        credible = [r for r in candidates[1:] if r[0] >= 0.9 * main[0]]
        picks = [(main[0], main[2])]
        # Prefer one complete supplier-friendly trade to a price-identical quality variant.
        # The cash estimate uses the published buyer rate as a declared hypothesis, not C_best.
        supplier = supplier_estimate(std, case["buyer"]["interest_rate"])
        def preference(row):
            p, c, s = row
            return (supplier_value(case, sheet(case, s, p), supplier), s.get("ppm_commitment", 0), -c)
        relevant = credible
        if index_series:
            relevant = [r for r in credible if (r[2].get("index_clause") or {}).get("series") == index_series]
        elif {"ppm_commitment", "chargeback_eur_per_complaint"} <= set(fields(case)):
            relevant = [r for r in credible if r[2]["chargeback_eur_per_complaint"] < main[2]["chargeback_eur_per_complaint"]]
        else:
            relevant = []
        if relevant and limit > 1:
            r = max(relevant, key=preference)
            picks.append((r[0], r[2]))
            if not index_series and len(picks) < limit:
                relaxed = max(relevant, key=lambda r: (r[2].get("ppm_commitment", 0),
                              -r[2].get("chargeback_eur_per_complaint", 0), preference(r)))
                if relaxed[2] not in [s for _, s in picks]:
                    picks.append((relaxed[0], relaxed[2]))
        varied = {f for _, s in picks[1:] for f in s if s[f] != main[2][f]}
        for p, _, s in credible:
            if len(picks) >= limit:
                break
            diff = {f for f in s if s[f] != main[2][f]}
            if len(diff) == 1 and not diff & varied and s not in [x[1] for x in picks]:
                # An earlier lead time at the same price adds no buyer value.
                if diff == {"lead_time_days"} and p == main[0] and s["lead_time_days"] < main[2]["lead_time_days"]:
                    continue
                picks.append((p, s)); varied |= diff
        return picks
    rows = []
    for s in grid(case):
        p = price_for(case, s, target_c, std)
        if ask:
            p = min(p, ask)
        t = sheet(case, s, p)
        if p > 0.01 and ok(case, t, std):
            rows.append((p, distance(case, s, current), s, cost(case, t, std)))
    # a structure whose C overshoots the target (its price floored at the bottom of the U, P03 at
    # 12 months) never beats one that meets it; then highest price, then closest to the supplier
    # keep only structures that meet the target; if none can (target below every U bottom), only
    # those within 0.5 % of the cheapest reachable C - never a 12-month sheet 254 k EUR over budget
    feasible = [r for r in rows if r[3] <= target_c + 0.01]
    if not feasible and rows:
        floor = min(r[3] for r in rows)
        feasible = [r for r in rows if r[3] <= floor * 1.005]
    rows = feasible
    rows.sort(key=lambda r: (-round(r[0], 2), round(r[3] / max(target_c, 1) * 1e4), r[1]))
    rows = [(p, d, s) for p, d, s, _ in rows]
    if not rows:
        return []
    main = rows[0][2]
    return [(rows[0][0], main)] + spread_alternatives(case, main, rows[0][0], [(p, s) for p, _, s in rows[1:]], current, limit - 1)


def spread_alternatives(case, main, main_price, candidates, current=None, n=2):
    """Up to `n` alternatives that each vary ONE lever of `main`, each lever once, choosing the
    option FARTHEST from main (its standard payment term at a lower price, the longest term, ...):
    the package the supplier picks then tells us what it values (MESO). Credible only: price
    within 10 % of the main sheet."""
    best = {}
    for p, s in candidates:
        diff = [f for f in s if s[f] != main[f]]
        if len(diff) != 1 or p < 0.9 * main_price:
            continue
        f = diff[0]
        d = distance(case, {f: s[f]}, {f: main[f]})
        if f not in best or d > best[f][0]:
            best[f] = (d, p, s)
    out = sorted(best.values(), key=lambda x: -x[1])[:n]
    return [(p, s) for _, p, s in out]


def best_at_price(case, price, std, current=None):
    """The structure that costs us least at a given price (never below the bottom of its U when
    a relationship charge applies), and that C; at equal cost (1 EUR) the one closest to the
    supplier's own terms - a change it did not ask for is a change it may charge for."""
    best, best_key = None, None
    for s in grid(case):
        p = price
        if (std or {}).get("_supplier"):
            p = max(p, cheapest_price(case, s, std, 0.01, max(10 * price, 1.0)))
        t = sheet(case, s, p)
        if ok(case, t, std):
            c = cost(case, t, std)
            key = (round(c), distance(case, s, current) if current else 0.0)
            if best is None or key < best_key:
                best, best_key = (c, s), key
    return best
