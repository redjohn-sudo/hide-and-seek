"""compile_evidence(case, data) -> list[Claim]: every verified argument of a case, strongest first.

Facts come from the package, the Sunday release (DataPackage merges `release-sunday` when it is
present) and the hidden case's own `facts`. Every claim is checked before it is returned.
Never raises: a family that fails is logged and contributes nothing.
"""

import logging

from hollerau.contracts import Claim

from .facts import check, facts_of
from .forecast_cost_news import forecast_cost_news_claims
from .index import index_claims
from .notice import notice_claims
from .orders import orders_claims
from .quote import quote_claims
from .scorecard import scorecard_claims

# order = strength: a contract breach, then a cheaper rival quote, the supplier's own record, cost and index
# data, our order history, then our forecast and the news
FAMILIES = [
    notice_claims,
    quote_claims,
    scorecard_claims,
    index_claims,
    orders_claims,
    forecast_cost_news_claims,
]

log = logging.getLogger(__name__)


def compile_evidence(case: dict, data) -> list[Claim]:
    case_id = case.get("case_id") if isinstance(case, dict) else None
    try:
        facts = facts_of(case, data)
    except Exception:
        log.exception("%s: facts unavailable, no claims", case_id)
        return []
    out, seen = [], set()
    for family in FAMILIES:
        try:
            claims = family(case, data, facts)
        except Exception:
            log.exception("%s: claim family %s failed, skipped", case_id, family.__name__)
            continue
        for claim in claims:
            if not check(claim, facts) or seen & set(claim.ids):  # each ID counts once per case
                continue
            seen |= set(claim.ids)
            out.append(claim)
    return out
