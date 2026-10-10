"""Quote family: a quote of this case (in its `refs` or our alternative) priced below what is at stake.

The arena checks the quote's price, same part (brief/api.md). A rival quote at or above the price at stake (the
lower of today's and the asked price) argues the supplier's case: it is never cited. Nor is a quote in another
price unit, or any quote in a bundle case (one price for a kit). Citing a rival quote reveals it to the supplier.
"""

from hollerau.contracts import Claim

from .facts import comparable, has, stake_price


def case_quotes(case: dict) -> list[str]:
    alt = (case["buyer"].get("alternative") or {}).get("ref") or ""
    refs = [r for r in case.get("refs", []) if r.startswith("quote:")] + ([alt] if alt.startswith("quote:") else [])
    return list(dict.fromkeys(refs))


def quote_claims(case: dict, data, facts: dict) -> list[Claim]:
    stake = stake_price(case, facts)
    if stake is None:
        return []
    found = [(q, facts[q]) for q in case_quotes(case) if has(facts.get(q), "unit_price")]
    found = [(q, f) for q, f in found if comparable(case, data, f) and f.get("supplier") not in case["suppliers"]]
    claims = []
    for q, f in sorted(found, key=lambda qf: qf[1]["unit_price"]):
        if f["unit_price"] >= stake:
            continue
        per = f" per {f['price_unit']} pcs" if (f.get("price_unit") or 1) > 1 else ""
        claims.append(
            Claim(
                family="quote",
                ids=[q],
                figure=f["unit_price"],
                unit="EUR",
                sentence=f"We hold a quote for the same part at {f['unit_price']:.2f} EUR{per} ({q}).",
                supports="price",
            )
        )
    return claims
