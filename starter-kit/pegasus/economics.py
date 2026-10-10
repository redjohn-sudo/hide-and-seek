"""Numbers for the negotiation, all from the published cost function - never from the model.

`grid` enumerates every allowed combination of the non-price negotiable fields; `price_for` finds
the unit price that makes a structure cost a given C; `structures` ranks structures by what they
cost us at the price that leaves the supplier indifferent (its cash value unchanged), which is how
we find trades that are cheap for it and valuable for us.
"""

from itertools import product

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


def cost(case, terms, std=None):
    return total_cost(case, terms, std or {})


def ok(case, terms, std=None):
    return must_haves_ok(case, terms, std or {})


def alt_cost(case):
    """C of the alternative (no deal), inf when there is none."""
    alt = case["buyer"].get("alternative") or {}
    if alt.get("kind") in (None, "none") or not (alt.get("terms") or {}).get("unit_price"):
        return float("inf")
    return total_cost(case, alt["terms"])


def price_for(case, structure, target_c, std=None, lo=0.01, hi=None):
    """Unit price at which `structure` costs `target_c` (C rises with the price)."""
    hi = hi or max(10 * (case["buyer"].get("fallback_unit_price") or 1), 1.0)
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
            rows.append((p, distance(case, s, current), s))
    rows.sort(key=lambda r: (-round(r[0], 2), r[1]))
    if not rows:
        return []
    main = rows[0][2]
    picks, varied = [(rows[0][0], main)], set()
    for p, _, s in rows[1:]:
        if len(picks) >= limit:
            break
        diff = {f for f in s if s[f] != main[f]}
        # an alternative must be a credible offer (no 12-month sheet at a fifth of the price)
        if len(diff) == 1 and not diff & varied and p >= 0.9 * picks[0][0]:
            picks.append((p, s))
            varied |= diff
    return picks


def best_at_price(case, price, std):
    """The structure that costs us least at a given price, and that C."""
    best = None
    for s in grid(case):
        t = sheet(case, s, price)
        if ok(case, t, std):
            c = cost(case, t, std)
            if best is None or c < best[0]:
                best = (c, s)
    return best
