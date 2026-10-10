"""Arguments the arena will verify: one sentence each, the figure next to its IDs.

Only claims that pass the arena's checks (api.md, "What counts") and that argue for the buyer are
built: a falling cost index of the part, a notice shorter than the contract's, a should-cost
estimate below the asking price, a growing volume forecast, a weak scorecard, a rival's lower
offer in an RFQ. Each ID is used once per case; at most three claims go into one message.
"""

import re
from datetime import date

MAX_PER_MESSAGE = 3
DRIVER_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+) rose\b")


def _month_minus(month, n):
    y, m = map(int, month.split("-"))
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


class Claims:
    def __init__(self, case, package):
        self.case, self.pkg = case, package
        self.used = set()

    def fact(self, ref):
        return (self.case.get("facts") or {}).get(ref) or (self.pkg.facts.get(ref) if self.pkg else None)

    # -- candidates -------------------------------------------------------------------------
    def index_claims(self, part, extra_series=()):
        """12-month changes of the part's cost-driver series that fell (they argue for us)."""
        if not self.pkg:
            return []
        series = {r["series_id"]: r["weight"] for r in self.pkg.query(
            "SELECT series_id, weight FROM cost_drivers WHERE part_id = ?", (part,))}
        for s in extra_series:
            series.setdefault(s, 0.0)
        out = []
        for s, weight in series.items():
            months = self.pkg.ids(f"idx:{s}@")
            if len(months) < 13:
                continue
            last = months[-1]
            first = f"idx:{s}@{_month_minus(last.split('@')[1], 12)}"
            a, b = self.fact(first), self.fact(last)
            if not (a and b and a.get("value")):
                continue
            change = (b["value"] / a["value"] - 1) * 100
            if change < -0.5:
                out.append({
                    "refs": [first, last], "weight": -change * (weight or 0.05),
                    "sentence": f"{s} changed {change:.1f}% from {first.split('@')[1]} to "
                                f"{last.split('@')[1]} ({first}, {last}).",
                })
        return sorted(out, key=lambda c: -c["weight"])

    def notice_claim(self):
        refs = self.case.get("refs", [])
        doc = next((r for r in refs if r.startswith("doc:") and (self.fact(r) or {}).get("days")), None)
        ctr = next((r for r in refs if r.startswith("ctr:") and (self.fact(r) or {}).get("days")), None)
        if doc and ctr and self.fact(doc)["days"] < self.fact(ctr)["days"]:
            return [{
                "refs": [doc, ctr], "weight": 50,
                "sentence": f"Your letter gives {self.fact(doc)['days']} days notice ({doc}), while our "
                            f"contract requires {self.fact(ctr)['days']} days ({ctr}).",
            }]
        return []

    def should_cost_claim(self, part, price):
        ref = f"cost:{part.split(':')[1]}"
        f = self.fact(ref)
        if f and price and f["unit_cost"] < price:
            return [{"refs": [ref], "weight": 30 * (price / f["unit_cost"] - 1) * 100,
                     "sentence": f"Our should-cost estimate for this part is {f['unit_cost']:.2f} EUR ({ref})."}]
        return []

    def forecast_claim(self, part):
        if not self.pkg:
            return []
        ids = self.pkg.ids(f"fc:{part.split(':')[1]}@")
        f = self.fact(ids[-1]) if ids else None
        if f and f.get("change_pct", 0) > 0.5:
            return [{"refs": [ids[-1]], "weight": 20,
                     "sentence": f"Our volume forecast for this part is up {f['change_pct']:.1f}% ({ids[-1]})."}]
        return []

    def scorecard_claim(self, supplier):
        if not self.pkg:
            return []
        ids = self.pkg.ids(f"sc:{supplier.split(':')[1]}@")
        f = self.fact(ids[-1]) if ids else None
        if not f:
            return []
        out = []
        if f.get("otd_pct") is not None and f["otd_pct"] < 90:
            out.append({"refs": [ids[-1]], "weight": 90 - f["otd_pct"],
                        "sentence": f"Your on-time delivery was {f['otd_pct']:.1f}% ({ids[-1]})."})
        elif f.get("ppm") is not None and f["ppm"] > 1000:
            out.append({"refs": [ids[-1]], "weight": f["ppm"] / 100,
                        "sentence": f"Your quality was {f['ppm']:.0f} ppm ({ids[-1]})."})
        return out

    def order_claim(self, supplier, part, price):
        """Our order history for this part at this supplier, lowest paid against the latest: the one
        order-history argument that moved a bot (P18: -0.60 instead of -0.40 at round 1), cited only
        when the lowest price is clearly under the current one (else "costs have gone up")."""
        if not self.pkg or not price:
            return []
        rows = [(k, v) for k, v in (getattr(self.pkg, 'facts', None) or {}).items() if k.startswith("po:") and isinstance(v, dict)
                and v.get("supplier") == supplier and v.get("part") == part and v.get("unit_price")
                and v.get("price_unit", 1) == 1]
        if len(rows) < 2:
            return []
        rows.sort(key=lambda kv: kv[1].get("date") or "")
        low = min(rows, key=lambda kv: kv[1]["unit_price"])
        latest = rows[-1]
        if (low[1]["unit_price"] >= 0.97 * price or low[0] == latest[0]
                or low[1]["unit_price"] >= 0.97 * latest[1]["unit_price"]):  # a flat history reads "costs went up"
            return []
        return [{"refs": [low[0], latest[0]], "weight": 60,
                 "sentence": (f"Our order history for this part shows {low[1]['unit_price']:.2f} EUR ({low[0]}) "
                              f"against {latest[1]['unit_price']:.2f} EUR in the latest order ({latest[0]}).")}]

    def rival_claim(self, rival_offer_id, price):
        return [{"refs": [rival_offer_id], "weight": 100, "rival": True,
                 "sentence": f"We hold a competing offer at {price:.2f} EUR per unit ({rival_offer_id})."}]

    # -- selection --------------------------------------------------------------------------
    def candidates(self, supplier, part, price, drivers_text=""):
        extra = DRIVER_RE.findall(drivers_text or "")
        out = (self.notice_claim() + self.index_claims(part, extra) + self.should_cost_claim(part, price)
               + self.forecast_claim(part) + self.scorecard_claim(supplier) + self.order_claim(supplier, part, price))
        return sorted(out, key=lambda c: -c["weight"])

    def pick(self, supplier, part, price, drivers_text="", rivals=(), rivals_only=False, limit=MAX_PER_MESSAGE):
        """Up to `limit` unused claims for the next message; marks their IDs as used."""
        pool = [c for r in rivals for c in self.rival_claim(*r)]
        if not rivals_only:
            pool += self.candidates(supplier, part, price, drivers_text)
        chosen = []
        for c in pool:
            key = tuple(c["refs"]) if c.get("rival") else None
            ids = set(c["refs"])
            if (key and (supplier, key) in self.used) or (not key and ids & self.used):
                continue
            chosen.append(c)
            if key:
                self.used.add((supplier, key))
            else:
                self.used |= ids
            if len(chosen) >= limit:
                break
        return chosen
