"""Turn what the supplier said (reader signals) into the next offer - general rules, no case data.

| signal (reader)        | next offer                                                          |
|------------------------|---------------------------------------------------------------------|
| requires_index_clause  | only structures carrying the clause it requires, widest deadband     |
| chargeback_too_high    | a chargeback strictly below the one it refused                      |
| lead_time_firm         | its own lead time, when the must-haves allow it                     |
| wants_longer_contract  | at equal cost for us, the longer term                               |
| wants_volume_commitment| at equal cost for us, the larger committed volume                   |
| arguments_rejected     | one argument at most in the next e-mail                             |
| cost_floor (twice)     | stop pushing: close                                                 |
Every rule only re-ranks or filters the case's own `term_options`; prices still come from C.
"""

from . import economics as eco

FLAGS = ("wants_index", "cb_too_high", "lead_firm", "wants_duration", "wants_volume")


def active(st):
    return any(st.get(k) for k in FLAGS) or bool(st.get("contract_lead"))


def index_series(case, st):
    """The index series the case allows - the one the supplier named if it named one."""
    opts = case.get("term_options", {}).get("index_clause") or []
    series = sorted({o["series"] for o in opts if isinstance(o, dict) and o.get("series")})
    named = [s for s in series if s in (st.get("drivers") or "")]
    return (named or series or [None])[0]


def allowed(case, st, s, current):
    if st.get("wants_index") and "index_clause" in s:
        ic, ser = s.get("index_clause"), index_series(case, st)
        if not isinstance(ic, dict) or (ser and ic.get("series") != ser):
            return False
    if st.get("cb_too_high") and "chargeback_eur_per_complaint" in s:
        cur = st.get("stated", {}).get("chargeback_eur_per_complaint", (current or {}).get("chargeback_eur_per_complaint"))
        if cur is not None and (s["chargeback_eur_per_complaint"] > 0 if cur <= 0 else s["chargeback_eur_per_complaint"] >= cur):
            return False
    if st.get("contract_lead") and "lead_time_days" in s and s["lead_time_days"] < st["contract_lead"]:
        return False  # shorter than the contract: a request it prices as a premium
    if st.get("lead_firm") and "lead_time_days" in s:
        cur = st.get("stated", {}).get("lead_time_days", (current or {}).get("lead_time_days"))
        if cur is not None and s["lead_time_days"] < cur:
            return False
    return True


def preference(case, st, s):
    """Tie-break among structures at the same price for us: lower is better."""
    key = []
    if st.get("wants_index") and isinstance(s.get("index_clause"), dict):
        key.append(-float(s["index_clause"].get("deadband_pct") or 0))  # widest deadband costs us least
    if st.get("wants_duration") and "contract_months" in s:
        key.append(-s["contract_months"])
    if st.get("wants_volume") and "volume_per_year" in s:
        key.append(-s["volume_per_year"])
    return tuple(key)


def pick_sheets(case, st, target, current, ask, limit=3):
    """Up to `limit` term sheets at our target C that honour the supplier's stated constraints."""
    rows = []
    for s in eco.grid(case):
        if not allowed(case, st, s, current):
            continue
        p = eco.price_for(case, s, target, st["std"])
        if ask:
            p = min(p, ask)
        t = eco.sheet(case, s, p)
        if p > 0.01 and eco.ok(case, t, st["std"]):
            rows.append((p, eco.cost(case, t, st["std"]), s))
    if not rows:
        return []
    feasible = [r for r in rows if r[1] <= target + 0.01]
    if not feasible:
        floor = min(r[1] for r in rows)
        feasible = [r for r in rows if r[1] <= floor * 1.005]
    feasible.sort(key=lambda r: (-round(r[0], 2), preference(case, st, r[2]), eco.distance(case, r[2], current)))
    main = feasible[0]
    picks, varied = [main], set()
    for p, c, s in feasible[1:]:
        if len(picks) >= limit:
            break
        diff = {f for f in s if s[f] != main[2][f]}
        if len(diff) == 1 and not diff & varied and p >= 0.9 * main[0]:
            picks.append((p, c, s))
            varied |= diff
    return [eco.sheet(case, s, p) for p, _, s in picks]


def claims_cap(st, default):
    return 1 if st.get("args_rejected") else default


def lines(case, st, sheets):
    """One sentence per constraint we honour, so the supplier sees it was heard."""
    out = []
    main = sheets[0] if sheets else {}
    if st.get("wants_index") and isinstance(main.get("index_clause"), dict):
        ic = main["index_clause"]
        out.append(f"We accept the {ic['series']} index clause on the material share with a "
                   f"{ic.get('deadband_pct', 0):g}% deadband; the attached base price reflects the price "
                   "risk this removes for you.")
    if st.get("cb_too_high") and "chargeback_eur_per_complaint" in main:
        out.append(f"We have reduced the chargeback to EUR {main['chargeback_eur_per_complaint']:g} per complaint.")
    if st.get("lead_firm") and "lead_time_days" in main:
        out.append(f"We can work with your lead time of {main['lead_time_days']} days.")
    if st.get("wants_duration") and main.get("contract_months"):
        out.append(f"The attached package gives you planning certainty over {main['contract_months']} months.")
    return out


def remaining_trade(case, st):
    """Some allowed value of a negotiable non-price field has never been offered: a trade remains."""
    proposed = st.get("proposed") or []
    for f in case.get("negotiable", []):
        if f == "unit_price":
            continue
        opts = case.get("term_options", {}).get(f) or []
        offered = [p.get(f) for p in proposed]
        if any(o not in offered for o in opts):
            return True
    return False


def must_close(st, case=None):
    """It said twice that its price is its limit and every trade has been tried: stop pushing."""
    return st.get("floor_hits", 0) >= 2 and not (case and remaining_trade(case, st))
