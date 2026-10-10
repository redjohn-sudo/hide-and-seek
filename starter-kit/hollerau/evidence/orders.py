"""Orders family: what our orders of the same part show - the supplier already raised its price, or, without a
price letter (renewal, raw material), the prices we pay at or below today's.

The arena checks 2-8 `po` lines of one part against their order prices (brief/api.md); lines of different
parts are separate arguments. We cite the first order of the last 24 months and the latest one, when the
price already rose by RAISED_PCT or more in between. Lines at an outdated price (`stale_condition`,
KNOWN_ISSUES 6) are skipped. Prices stay per the part's price_unit, as in the data.
"""

from datetime import date

from hollerau.contracts import Claim

from .facts import is_bundle, package, price_letter, release_rows

WINDOW_DAYS = 730
SILENT_TYPES = {"rfq"}  # an RFQ: our order prices would tell the rival bidders what we pay
RAISED_PCT = 3.0
COLUMNS = "id, order_date, price, price_unit, price_note, part_id, supplier_id"


def _row(r: dict) -> dict | None:
    """An order line with a usable date and price (release CSV rows hold strings), else None."""
    try:
        price, unit = float(r["price"]), int(float(r.get("price_unit") or 1))
        return {**r, "price": price, "price_unit": unit, "day": date.fromisoformat(r["order_date"])}
    except (KeyError, TypeError, ValueError):
        return None


def _lines(data, part: str, supplier: str, facts: dict) -> list[dict]:
    """The supplier's order lines for the part, package then Sunday release, oldest first."""
    p = package(data)
    rows = []
    if p is not None and p.db is not None:
        rows = p.query(
            f"SELECT {COLUMNS} FROM po_lines WHERE part_id = ? AND supplier_id = ? ORDER BY order_date, id",
            (part, supplier),
            limit=10_000,
        )
    rows += [r for r in release_rows(data, "po_lines") if r.get("part_id") == part and r.get("supplier_id") == supplier]
    lines = [x for r in rows if r.get("price_note") != "stale_condition" and r.get("id") in facts and (x := _row(r))]
    return sorted((x for x in lines if x["price"] > 0), key=lambda x: (x["day"], x["id"]))


def _per(line: dict) -> str:
    return f" per {line['price_unit']} pcs" if (line.get("price_unit") or 1) > 1 else ""


def _rise_claim(part: str, lines: list[dict]) -> Claim | None:
    """The price already rose by RAISED_PCT or more over the last 24 months: first line of the window, last line."""
    last = lines[-1]
    since = last["day"].toordinal() - WINDOW_DAYS
    first = next(r for r in lines if r["day"].toordinal() >= since)
    if first is last or (last["price"] / first["price"] - 1) * 100 < RAISED_PCT:
        return None
    return Claim(
        family="orders",
        ids=[first["id"], last["id"]],
        figure=None,  # the arena takes the order prices themselves
        unit="",
        sentence=(
            f"Our orders of {part} already went from {first['price']:.2f} ({first['id']}, "
            f"{first['order_date'][:7]}) to {last['price']:.2f} EUR{_per(last)} ({last['id']}, "
            f"{last['order_date'][:7]})."
        ),
        supports="price",
    )


def _held_claim(part: str, lines: list[dict], current: float) -> Claim | None:
    """Without a price letter: the lowest and the latest order of the last 24 months, both at or below today's
    price. They show what we pay and argue for keeping it."""
    last = lines[-1]
    since = last["day"].toordinal() - WINDOW_DAYS
    window = [r for r in lines if r["day"].toordinal() >= since and r["price"] <= current]
    if last not in window:
        return None
    low = min(window, key=lambda r: (r["price"], r["day"]))
    if low is last:
        low = next((r for r in window if r is not last and r["price"] == last["price"]), None)
    if low is None:
        return None
    return Claim(
        family="orders",
        ids=[low["id"], last["id"]],
        figure=None,
        unit="",
        sentence=(
            f"Our orders of {part} were placed at {low['price']:.2f} ({low['id']}, {low['order_date'][:7]}) "
            f"and {last['price']:.2f} EUR{_per(last)} ({last['id']}, {last['order_date'][:7]})."
        ),
        supports="price",
    )


def orders_claims(case: dict, data, facts: dict) -> list[Claim]:
    """Per part (one argument each): the price already rose; else, without a price letter, what we pay today.
    Never in a bundle (the case price is the kit's, not one part's), never in an RFQ (it reveals our price)."""
    if is_bundle(case) or case.get("type") in SILENT_TYPES:
        return []
    current = (case.get("buyer") or {}).get("current_unit_price")  # without it, only the rise claim
    held = not price_letter(case, facts) and current is not None
    claims = []
    for supplier in case["suppliers"]:
        for part in case["parts"]:
            lines = _lines(data, part, supplier, facts)
            if len(lines) < 2:
                continue
            claim = _rise_claim(part, lines) or (_held_claim(part, lines, current) if held else None)
            if claim:
                claims.append(claim)
    return claims
