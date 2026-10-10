"""forecast_cost_news family: our demand forecast (`fc`), our should-cost (`cost`), news about the supplier, and the
supplier's own letter saying its material is covered (`doc`).

The arena checks the recorded change of an `fc` (a %), the estimate of a `cost` (EUR), and that a `news`
item is about this supplier (brief/api.md). Guards:
- `cost`: cited only when the price at stake (the lower of today's and the asked price) exceeds it by more than
  the should-cost's own error (5-15 %, KNOWN_ISSUES 14), and only when comparable (one part, same price unit).
- `fc`: our latest forecast for the part, cited when it grows: volume is our leverage. Never in a capacity case or
  with a supplier whose order book is full or that allocates: there, more volume is the supplier's leverage.
- `news`: only topics that weaken the supplier's position (free capacity, a new plant), from the 12 months before
  the case starts (Sunday release included, even after the supplier's letter).
- `doc` (material covered, signal material_hedged): supports `price` - while its costs are fixed, no increase is
  justified. Only without a price letter (neither asked price nor asked %), only the supplier's own letters and
  e-mails (our call and meeting notes are context, not checks), only if the cover runs beyond the effective date,
  and never if the index path of the case's index series falls (then "market moves reach us only later" is the
  supplier's argument against a decrease). The arena has no row for a letter cited alone: its result is not known
  yet (no figure, so at worst `irrelevant`).

Each sub-family runs on its own: one that fails is logged and the others still count.
"""

import logging
from datetime import date, datetime, timedelta

from arena_kit.cost import horizon_start
from hollerau.contracts import Claim

from .facts import _day, case_start, comparable, has, package, price_letter, stake_price
from .signals import HEDGED, saturated

COST_ERROR = 0.15
FC_GROWTH_PCT = 5.0
NEWS_TOPICS = {"free_capacity", "new_plant"}
NEWS_DAYS = 365
CAPACITY_TYPES = {"capacity"}

log = logging.getLogger(__name__)


def _number(ref: str) -> str:
    return ref.split(":", 1)[1]


def cost_claims(case: dict, data, facts: dict) -> list[Claim]:
    stake = stake_price(case, facts)
    claims = []
    for part in case["parts"]:
        cid = f"cost:{_number(part)}"
        row = facts.get(cid)
        if not has(row, "unit_cost") or stake is None or not comparable(case, data, row):
            continue
        if row["unit_cost"] * (1 + COST_ERROR) >= stake:
            continue
        claims.append(
            Claim(
                family="forecast_cost_news",
                ids=[cid],
                figure=row["unit_cost"],
                unit="EUR",
                sentence=f"Our should-cost estimate for {part} is {row['unit_cost']:.2f} EUR ({cid}).",
                supports="price",
            )
        )
    return claims


def forecast_claims(case: dict, data, facts: dict) -> list[Claim]:
    if case.get("type") in CAPACITY_TYPES or any(saturated(case, data, facts, s) for s in case["suppliers"]):
        return []
    claims = []
    for part in case["parts"]:
        ids = sorted(i for i in facts if i.startswith(f"fc:{_number(part)}@") and has(facts[i], "change_pct"))
        if not ids or facts[ids[-1]]["change_pct"] < FC_GROWTH_PCT:
            continue
        fid, change = ids[-1], facts[ids[-1]]["change_pct"]
        claims.append(
            Claim(
                family="forecast_cost_news",
                ids=[fid],
                figure=change,
                unit="%",
                sentence=f"Our latest forecast for {part} grows {change:+.1f}% ({fid}).",
                supports="volume",
            )
        )
    return claims


def news_claims(case: dict, facts: dict) -> list[Claim]:
    until = case_start(case, facts)
    since = until - timedelta(days=NEWS_DAYS) if until else None
    claims = []
    for nid in sorted((i for i in facts if i.startswith("news:")), reverse=True):
        n = facts[nid]
        if not has(n, "date", "headline", "supplier", "topic"):
            continue
        if n["supplier"] not in case["suppliers"] or n["topic"] not in NEWS_TOPICS:
            continue
        day = _day(n["date"])
        if day is None or (since and day < since) or (until and day > until):
            continue
        claims.append(
            Claim(
                family="forecast_cost_news",
                ids=[nid],
                figure=None,
                unit="",
                sentence=f"We read that {n['headline'].rstrip('.')} ({nid}).",
                supports="volume" if n["topic"] == "free_capacity" else "other",
            )
        )
    return claims


SUPPLIER_DOCS = {"letter", "email"}


def _until(text: str) -> tuple[str, date] | None:
    """('March 2027', 2027-03-01) from "... at fixed prices until March 2027 ...", else None."""
    if not (m := HEDGED.search(text)):
        return None
    try:
        return m.group(1), datetime.strptime(m.group(1), "%B %Y").date()
    except ValueError:
        return None


def _index_falls(case: dict) -> bool:
    """The case's index path falls for a series of its index clause (any series when the case names none)."""
    paths = (case.get("buyer") or {}).get("index_paths") or {}
    clauses = [
        *(case.get("term_options", {}).get("index_clause") or []),
        case.get("fixed_terms", {}).get("index_clause"),
    ]
    series = {c["series"] for c in clauses if isinstance(c, dict) and c.get("series")}
    moves = [paths[s] for s in series if paths.get(s) is not None] if series else list(paths.values())
    return any(m is not None and m < 0 for m in moves)


def letter_claims(case: dict, data, facts: dict) -> list[Claim]:
    """The supplier's own letter or e-mail saying its material is covered beyond the effective date, without a price
    letter (raw material, renewal). No figure, supports `price`: while its costs are fixed, no increase is justified."""
    if price_letter(case, facts) or _index_falls(case):
        return []
    p, start = package(data), case_start(case, facts)
    if p is None:
        return []
    effective = horizon_start(case) or start  # when the new price takes effect, else the case start
    claims = []
    for doc_id in sorted(i for i in facts if i.startswith("doc:")):
        doc = facts[doc_id]
        if not has(doc, "supplier", "date", "type") or doc["supplier"] not in case["suppliers"]:
            continue
        sent = _day(doc["date"])
        if doc["type"] not in SUPPLIER_DOCS or sent is None or (start and sent > start):
            continue
        try:
            hedge = _until(" ".join(p.document(doc_id).split()))
        except (FileNotFoundError, OSError, ValueError):
            continue
        if hedge is None or (effective and hedge[1] < effective.replace(day=1)):
            continue  # no cover, or it ends before the new price takes effect
        claims.append(
            Claim(
                family="forecast_cost_news",
                ids=[doc_id],
                figure=None,
                unit="",
                sentence=f"Your letter of {doc['date']} ({doc_id}) says your material is covered until {hedge[0]}.",
                supports="price",
            )
        )
    return claims


def forecast_cost_news_claims(case: dict, data, facts: dict) -> list[Claim]:
    parts = {
        "should-cost": lambda: cost_claims(case, data, facts),
        "letter": lambda: letter_claims(case, data, facts),
        "forecast": lambda: forecast_claims(case, data, facts),
        "news": lambda: news_claims(case, facts),
    }
    claims = []
    for name, part in parts.items():
        try:
            claims += part()
        except Exception:
            log.exception("%s: forecast_cost_news %s failed, skipped", case.get("case_id"), name)
    return claims
