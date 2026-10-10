"""Load the data package and check a claim the way the arena does (brief/api.md, « What counts »).

Prices in the data and in the case file are both per the part's `price_unit` (per 1, 100 or 1000 pieces):
compare them as they are, never divided (P14: case price 316.35 = last order line 316.35, per 100). A row in
another price unit, or a bundle (one price for a kit of parts), is not comparable with the case price at all.

Never raise out of this lane: a fact with a missing or null field is skipped, a check that cannot be made is False.
"""

import csv
import logging
import os
from datetime import date
from pathlib import Path
from statistics import mean

from arena_kit.data import DataPackage
from hollerau.contracts import Claim

log = logging.getLogger(__name__)

# Where the package usually sits: ARENA_DATA, this repo's data/package (`uv run arena data`), or a kit clone.
REPO_PACKAGE = Path(__file__).resolve().parents[2] / "data" / "package"
KIT_PACKAGE = Path("~/Projets/negotiation-arena-kit/starter-kit/data/package").expanduser()

PCT_TOL = 0.5  # percentage points
REL_TOL = 0.005  # ±0.5 %, at least 0.01
MAX_ORDERS = 8
BUNDLE_TYPES = {"bundling"}

_loaded: DataPackage | None = None  # only a package that loaded is kept: a failure is retried on the next call


def load_package() -> DataPackage:
    for c in (os.environ.get("ARENA_DATA"), REPO_PACKAGE, KIT_PACKAGE):  # ARENA_DATA read at call time
        if c and (Path(c) / "facts.json").is_file():
            return DataPackage(c)
    raise FileNotFoundError("no data package: run `uv run arena data` or set ARENA_DATA")


def package(data: DataPackage | None = None) -> DataPackage | None:
    """`data` when given (pass it in the final: package + Sunday release), else the package on disk, else None."""
    global _loaded
    if data is not None:
        return data
    if _loaded is None:
        try:
            _loaded = load_package()
        except FileNotFoundError:
            log.warning("no data package found: facts are the case's own only")
            return None
    return _loaded


def facts_of(case: dict, data: DataPackage | None = None) -> dict:
    """Every fact citable in this case: package, Sunday release, and the case's own `facts`."""
    p = package(data)
    base = p.facts if p is not None else {}
    own = case.get("facts") if isinstance(case.get("facts"), dict) else {}
    return base | own


def has(row, *keys) -> bool:
    """True if `row` is a dict whose `keys` are all present and not null."""
    return isinstance(row, dict) and all(row.get(k) is not None for k in keys)


def asked_price(case: dict, facts: dict) -> float | None:
    """The price the supplier's letter asks for (`asked_price` of the trigger doc), if any."""
    doc = facts.get(case.get("trigger_doc") or "")
    return doc.get("asked_price") if isinstance(doc, dict) else None


def price_letter(case: dict, facts: dict) -> bool:
    """The supplier's letter asks for a price change: an `asked_price` or an `asked_pct` (either is enough)."""
    doc = facts.get(case.get("trigger_doc") or "")
    return isinstance(doc, dict) and (doc.get("asked_price") is not None or doc.get("asked_pct") is not None)


def stake_price(case: dict, facts: dict) -> float | None:
    """The price a rival quote or our should-cost must beat to be worth citing: the lower of the current price and
    the asked price. A rival above today's price would anchor the supplier there."""
    prices = [p for p in (case.get("buyer", {}).get("current_unit_price"), asked_price(case, facts)) if p]
    return min(prices) if prices else None


def is_bundle(case: dict) -> bool:
    return case.get("type") in BUNDLE_TYPES or len(case.get("parts") or []) > 1


def part_price_unit(data, part: str) -> int:
    """The part's price unit (parts table), 1 when unknown."""
    p = package(data)
    if p is None or p.db is None:
        return 1
    rows = p.query("SELECT price_unit FROM parts WHERE id = ?", (part,))
    return (rows[0]["price_unit"] if rows else None) or 1


def comparable(case: dict, data, row: dict) -> bool:
    """A quote or should-cost row can be set against the case price: one part (no kit), same price unit."""
    if is_bundle(case) or not has(row, "part") or row["part"] not in (case.get("parts") or []):
        return False
    return (row.get("price_unit") or 1) == part_price_unit(data, row["part"])


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def case_start(case: dict, facts: dict) -> date | None:
    """When the case starts: its own `start_date`, else the latest dated fact (package + Sunday release). News and
    letters up to then are known to us, even when they are later than the supplier's letter."""
    if d := _day(case.get("start_date")):
        return d
    days = [d for f in facts.values() if isinstance(f, dict) and (d := _day(f.get("date")))]
    return max(days) if days else None


def release_rows(data, table: str) -> list[dict]:
    """Rows of a Sunday-release table (`data/<table>.csv`): the release is not in the SQLite file."""
    p = package(data)
    path = p.release / "data" / f"{table}.csv" if p is not None and p.release else None
    if path is None or not path.is_file():
        return []
    try:
        with path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        log.exception("cannot read release table %s", table)
        return []


def series_change_pct(facts: dict, earlier: str, later: str) -> float:
    a, b = facts[earlier], facts[later]
    if a["series"] != b["series"]:
        raise ValueError("an index claim compares one series")
    return (b["value"] / a["value"] - 1) * 100


def close(figure: float, truth: float, unit: str) -> bool:
    if unit == "%":
        return abs(figure - truth) <= PCT_TOL
    return abs(figure - truth) <= max(abs(truth) * REL_TOL, 0.01)


def _orders_ok(claim: Claim, facts: dict) -> bool:
    rows = [facts[i] for i in claim.ids]
    return (
        2 <= len(rows) <= MAX_ORDERS
        and all(i.startswith("po:") for i in claim.ids)
        and len({r["part"] for r in rows}) == 1
    )


def _fcn_ok(claim: Claim, facts: dict) -> bool:
    """forecast_cost_news: one `fc` (change %), one `cost` (estimate EUR), one `news` or one supplier `doc` (no
    figure)."""
    if len(claim.ids) != 1:
        return False
    i, row = claim.ids[0], facts[claim.ids[0]]
    if i.startswith("news:"):
        return claim.figure is None
    if i.startswith("doc:"):  # the supplier's own letter (material covered), cited without a figure
        return claim.figure is None and row.get("type") in {"letter", "email"}
    if claim.figure is None:
        return i.startswith(("fc:", "cost:"))
    if i.startswith("fc:"):
        return claim.unit == "%" and close(claim.figure, row["change_pct"], "%")
    if i.startswith("cost:"):
        return claim.unit == "EUR" and close(claim.figure, row["unit_cost"], "EUR")
    return False


def _check(claim: Claim, facts: dict) -> bool:
    if not claim.ids or any(i not in facts for i in claim.ids):
        return False
    family, fig = claim.family, claim.figure
    if family == "orders":
        return _orders_ok(claim, facts)  # checked against the order prices: no figure of ours
    if family == "forecast_cost_news":
        return _fcn_ok(claim, facts)
    if fig is None:
        return True
    if family == "index":
        return len(claim.ids) == 2 and close(fig, series_change_pct(facts, *claim.ids), "%")
    if family == "notice":
        doc = next((facts[i] for i in claim.ids if i.startswith("doc:")), None)
        ctr = next((facts[i] for i in claim.ids if i.startswith("ctr:")), None)
        return has(doc, "days") and has(ctr, "days") and doc["days"] == fig and doc["days"] < ctr["days"]
    if family == "quote":
        return (
            len(claim.ids) == 1
            and claim.ids[0].startswith("quote:")
            and close(fig, facts[claim.ids[0]]["unit_price"], "EUR")
        )
    if family == "scorecard":
        rows = [facts[i] for i in claim.ids]
        if not all(i.startswith(("sc:", "qn:")) for i in claim.ids):
            return False
        if claim.unit == "ppm":
            return close(fig, mean(r["ppm"] for r in rows), "ppm")
        if claim.unit == "%":
            return all(i.startswith("sc:") for i in claim.ids) and close(fig, mean(r["otd_pct"] for r in rows), "%")
        return False
    return False  # a family the arena does not check: never send it


def check(claim: Claim, facts: dict) -> bool:
    """True if every ID exists and the figure matches the data within the arena's tolerance. Never raises: a fact
    with a missing or null field makes the claim unverifiable, so False."""
    try:
        return bool(_check(claim, facts))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return False
