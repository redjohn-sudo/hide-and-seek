"""Last check before a message leaves: fail closed, never send something we cannot stand behind.

Two ideas from the team's earlier projects:
- oblivion `LotFige`: every citation is checked against the source it was taken from - here every
  ID in the message must exist (package facts, case facts or an offer of this session) and every
  figure next to an ID must equal the fact's value within the arena's tolerance;
- bad-code `proteger_sortie`: a leak filter that blocks rather than hopes - here our alternative,
  its price, a budget or urgency never reach a supplier.
A message that fails loses the offending sentences; if a term sheet is invalid the message is
replaced by a safe question. `check` returns (message, problems).
"""

import re

from arena_kit.cost import must_haves_ok

ID_RE = re.compile(r"\b(?:idx|quote|offer|sc|qn|po|cost|fc|news|doc|ctr):[\w.:@#/-]*\w")
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
LEAK_WORDS = re.compile(r"\b(urgent|urgently|deadline|budget|no alternative|only option|asap|desperate)\b", re.I)


def _known(ref, facts, case, offers):
    return ref in offers or ref in (case.get("facts") or {}) or ref in facts


def _figure_ok(sentence, refs, facts):
    """The figure written next to the IDs matches the facts (arena tolerance: ±0.5 pt for %, else ±0.5 %)."""
    vals = [facts.get(r) or {} for r in refs]
    words = re.sub(r"\b[a-z]+:[\w.:@#/-]+", "", sentence)  # any ID-like token (part:, sup:, ...)
    words = re.sub(r"\b\d{4}-\d{2}\b", "", words)            # months are wording, not figures
    nums = [float(x) for x in NUM_RE.findall(words)]
    if not nums:
        return True
    if len(refs) == 2 and all("value" in v for v in vals):  # an index change in %
        change = (vals[1]["value"] / vals[0]["value"] - 1) * 100
        return any(abs(n - change) <= 0.5 for n in nums)
    expected = []
    for v in vals:
        for k in ("unit_cost", "unit_price", "change_pct", "otd_pct", "ppm", "days"):
            if v.get(k) is not None:
                expected.append(float(v[k]))
    if not expected:
        return True
    return all(any(abs(n - e) <= max(abs(e) * 0.005, 0.01) or abs(n - e) <= 0.5 and abs(e) < 100 for e in expected)
               for n in nums if n not in (12,))  # "12 months" is wording, not a figure


def check(case, message, facts, offers=(), ask=None, alt_price=None):
    problems = []
    if message.get("action"):
        return message, problems
    offers = set(offers)
    # 1. term sheets: complete, valid, never above what the supplier asks for the same thing
    for ts in message.get("term_sheets") or []:
        t = ts.get("terms", {})
        price = t.get("unit_price")
        if set(t) != set(case["negotiable"]) or price is None or not (price == price) or price <= 0:
            problems.append(f"invalid sheet {t}")
        elif not must_haves_ok(case, t):
            problems.append(f"sheet breaks a must-have {t}")
        elif ask and price > ask * 1.25:
            problems.append(f"price {price} far above the supplier's ask {ask}")
    if problems:
        return {"subject": message.get("subject", ""), "body": "Thank you for your offer. "
                "Could you confirm your earliest lead time and your best price?"}, problems
    # 2. sentences: every ID known, every figure true, nothing leaked
    kept, dropped_refs = [], set()
    for line in (message.get("body") or "").split("\n"):
        refs = ID_RE.findall(line)
        bad = [r for r in refs if not _known(r, facts, case, offers)]
        if bad:
            problems.append(f"unknown IDs {bad}")
            dropped_refs |= set(refs)
            continue
        if refs and not any(r.startswith("offer:") for r in refs) and not _figure_ok(line, refs, facts):
            problems.append(f"figure does not match the facts: {line}")
            dropped_refs |= set(refs)
            continue
        if LEAK_WORDS.search(line) or (alt_price and f"{alt_price:.2f}" in line):
            problems.append(f"leak: {line}")
            continue
        kept.append(line)
    out = dict(message, body="\n".join(kept))
    if message.get("refs"):
        out["refs"] = [r for r in message["refs"] if r not in dropped_refs]
    return out, problems
