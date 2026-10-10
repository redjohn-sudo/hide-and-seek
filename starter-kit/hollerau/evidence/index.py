"""Index family: one claim per cost-driver series that moved less than the supplier's demand.

The arena checks two months of ONE series, at most 12 months apart, ending within the last 12
months of the data. We use the latest 12-month window, as Tacto's house agent does
(tests/fixtures/sessions/P01-naive.json). The weighted move over all drivers is not citable:
it decides whether the demand is justified (signal) and goes in prose without IDs.
"""

from dataclasses import dataclass

from hollerau.contracts import Claim

WINDOW_MONTHS = 12
UNDERCUT = 0.5  # a series supports us only if it fell, or rose by less than half the demand
JUSTIFIED_MARGIN = 1.0  # points: a weighted move within 1 point of the demand already justifies it (P17: 7.95 / 8)

SHORT_NAMES = {"LCI-C": "labour costs", "CU-NOTATION": "copper notation"}


def short_name(series: str, name: str) -> str:
    """A name that fits in one sentence: 'electric power generation producer prices', 'labour costs'."""
    if series in SHORT_NAMES:
        return SHORT_NAMES[series]
    if series.startswith("AS-"):
        return f"alloy surcharge {series[3:]}"
    base = name.split(" (")[0].split(",")[0]
    if base.startswith("Producer prices DE: "):
        return f"{base.removeprefix('Producer prices DE: ')} producer prices"
    return base


@dataclass
class SeriesMove:
    series: str
    name: str
    weight: float
    earlier: str  # idx:...@YYYY-MM
    later: str
    change_pct: float


def shift(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    t = y * 12 + (m - 1) + delta
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def driver_moves(part: str, data, facts: dict) -> list[SeriesMove]:
    """The latest 12-month move of every cost-driver series of a part."""
    out = []
    names = {r["id"]: r["name"] for r in data.query("SELECT id, name FROM index_series")}
    for d in data.query("SELECT series_id, weight FROM cost_drivers WHERE part_id = ?", (part,)):
        months = sorted(i for i in facts if i.startswith(f"idx:{d['series_id']}@"))
        if not months:
            continue
        later = months[-1]
        earlier = f"idx:{d['series_id']}@{shift(later.rsplit('@', 1)[1], -WINDOW_MONTHS)}"
        a, b = (facts.get(earlier) or {}).get("value"), (facts.get(later) or {}).get("value")
        if not a or b is None or d.get("weight") is None:  # missing month or null value: no move
            continue
        out.append(
            SeriesMove(
                d["series_id"],
                names.get(d["series_id"], d["series_id"]),
                d["weight"],
                earlier,
                later,
                (b / a - 1) * 100,
            )
        )
    return out


def asked_pct(case: dict, facts: dict) -> float | None:
    """The increase the supplier's letter asks for, if the case has one."""
    doc = facts.get(case.get("trigger_doc") or "")
    return doc.get("asked_pct") if doc else None


def weighted_move_pct(moves: list[SeriesMove]) -> float:
    return sum(m.weight * m.change_pct for m in moves)


def increase_justified(weighted: float, asked: float) -> bool:
    """The cost drivers explain the demand, to within JUSTIFIED_MARGIN points: citing indices would argue the
    supplier's case."""
    return weighted >= asked - JUSTIFIED_MARGIN


def index_claims(case: dict, data, facts: dict) -> list[Claim]:
    """Claims for series that undercut the demand. None if the demand is justified overall."""
    asked = asked_pct(case, facts)
    claims = []
    for part in case["parts"]:
        moves = driver_moves(part, data, facts)
        if not moves:
            continue
        if asked is not None and increase_justified(weighted_move_pct(moves), asked):
            continue  # the increase is justified: citing indices would argue the supplier's case
        for m in sorted(moves, key=lambda m: m.change_pct):
            # Guard: a series that rose by half the demand or more (or rose at all, without a demand)
            # argues the supplier's case.
            if (asked is not None and m.change_pct >= UNDERCUT * asked) or (asked is None and m.change_pct >= 0):
                continue
            fig = round(m.change_pct, 1)
            claims.append(
                Claim(
                    family="index",
                    ids=[m.earlier, m.later],
                    figure=fig,
                    unit="%",
                    sentence=(
                        f"{short_name(m.series, m.name).capitalize()} ({m.series}) changed {fig:+.1f}% "
                        f"from {m.earlier.rsplit('@', 1)[1]} to {m.later.rsplit('@', 1)[1]} "
                        f"({m.earlier}, {m.later})."
                    ),
                    supports="price",
                )
            )
    return claims
