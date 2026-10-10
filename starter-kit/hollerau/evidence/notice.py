"""Notice family: the supplier's letter gives less notice than the contract requires.

The arena checks a letter `doc` giving notice against the contract `ctr`: the notice the letter
gives, against the contract's, in days (brief/api.md). Our figure is the letter's notice.
"""

from hollerau.contracts import Claim


def contract_for(case: dict, supplier: str, facts: dict) -> str | None:
    """The contract the case points to for this supplier, else the supplier's contract in the facts."""
    refs = [r for r in case.get("refs", []) if r.startswith("ctr:") and "#" not in r]
    for r in refs:
        if facts.get(r, {}).get("supplier") == supplier:
            return r
    own = sorted(i for i, f in facts.items() if i.startswith("ctr:") and "#" not in i and f.get("supplier") == supplier)
    return own[0] if own else None


def notice_claims(case: dict, data, facts: dict) -> list[Claim]:
    doc_id = case.get("trigger_doc")
    doc = facts.get(doc_id or "")
    if not doc or doc.get("days") is None:
        return []
    ctr_id = contract_for(case, doc.get("supplier"), facts)
    agreed = (facts.get(ctr_id or "") or {}).get("days")
    if agreed is None or doc["days"] >= agreed:
        return []
    return [
        Claim(
            family="notice",
            ids=[doc_id, ctr_id],
            figure=float(doc["days"]),
            unit="days",
            sentence=(
                f"Your letter gives {doc['days']} days' notice ({doc_id}), while our contract requires "
                f"{agreed} days ({ctr_id}), so the change cannot take effect on the date you announce."
            ),
            supports="effective_date",
        )
    ]
