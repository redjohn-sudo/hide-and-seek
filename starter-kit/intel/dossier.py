"""Faits citables exacts, contexte ERP et estimations explicitement séparés.

Les calculs ne sont jamais présentés comme des valeurs de facts.json.
Les règles portent sur les signaux observés, jamais sur une liste de fournisseurs.
"""

import copy
import csv
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path


def context(value, table, key, column):
    return {"kind": "context", "value": value, "citable": False,
            "source": {"table": table, "key": key, "column": column}}


def derived(value, formula, inputs, **extra):
    return {"kind": "derived", "value": value, "citable": False,
            "formula": formula, "inputs": inputs, **extra}


def month_shift(month, offset):
    year, mon = map(int, month.split("-"))
    n = year * 12 + mon - 1 + offset
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def _cell(value, typ):
    if value in (None, ""):
        return None
    if typ == "integer":
        return int(value)
    if typ == "number":
        return float(value)
    if typ == "boolean":
        return int(str(value).lower() in ("true", "1"))
    return value


class Package:
    """Lecture seule du SQLite ou des CSV, puis fusion additive de la release.

    L'objet peut être réutilisé directement pour préparer plusieurs dossiers.
    Les tables et facts sont internes : ne pas les modifier.
    """

    def __init__(self, package_dir, release_dir=None):
        self.root = Path(package_dir).expanduser().resolve()
        self.release = self.find_release(self.root, release_dir)
        self.facts = json.loads((self.root / "facts.json").read_text(encoding="utf-8"))
        meta_path = self.root / "datapackage.json"
        self.meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        self.resources = {r["name"]: r for r in self.meta.get("resources", [])}
        description = self.meta.get("description", "")
        found = re.search(r"as of (\d{4}-\d{2}-\d{2})", description)
        self.as_of = found[1] if found else "2026-09-30"
        self.tables = {}
        db = self.root / "world.sqlite"
        if db.is_file():
            connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only=ON")
                names = [r[0] for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                for name in names:
                    quoted = name.replace('"', '""')
                    self.tables[name] = [dict(r) for r in connection.execute(f'SELECT * FROM "{quoted}"')]
            finally:
                connection.close()
        else:
            for path in sorted((self.root / "data").glob("*.csv")):
                self.tables[path.stem] = self.read_csv(path)
        if self.release:
            additions = json.loads((self.release / "facts.json").read_text(encoding="utf-8"))
            for ref, fact in additions.items():
                if ref in self.facts and self.facts[ref] != fact:
                    raise ValueError(f"release incompatible : {ref}")
                self.facts[ref] = fact
            for path in sorted((self.release / "data").glob("*.csv")):
                target = self.tables.setdefault(path.stem, [])
                known = {r.get("id"): r for r in target if r.get("id")}
                for row in self.read_csv(path):
                    if row.get("id") in known:
                        if row != known[row["id"]]:
                            raise ValueError(f"ligne release incompatible : {row['id']}")
                        continue
                    row["_root"] = str(self.release)
                    target.append(row)
            dates = [f.get("date") for f in additions.values() if f.get("date")]
            if dates:
                self.as_of = max(self.as_of, max(dates))
        # Release réduite à facts + fichiers : conserver aussi ces documents/news.
        for table, prefix in (("documents", "doc:"), ("news", "news:")):
            rows = self.tables.setdefault(table, [])
            present = {r["id"] for r in rows}
            for ref, fact in self.facts.items():
                if ref.startswith(prefix) and ref not in present:
                    rows.append({"id": ref, "supplier_id": fact.get("supplier"),
                                 "date": fact.get("date", ""), **fact,
                                 "_root": str(self.release or self.root)})
        self.by = {}
        self.ids = {}
        for table, rows in self.tables.items():
            for row in rows:
                if row.get("id"):
                    self.ids[row["id"]] = (table, row)
            for column in ("supplier_id", "part_id", "contract_id", "rfq_id", "series_id", "fc_id"):
                index = defaultdict(list)
                for row in rows:
                    if column in row:
                        index[row[column]].append(row)
                self.by[table, column] = index
        self.aliases = {}
        issues = self.root / "KNOWN_ISSUES.md"
        if issues.is_file():
            for old, new in re.findall(r"`(sup:\d+)` is the old record of `(sup:\d+)`", issues.read_text()):
                self.aliases[old] = new
        self.index_facts = defaultdict(dict)
        for ref, fact in self.facts.items():
            if ref.startswith("idx:"):
                self.index_facts[fact["series"]][fact["month"]] = ref
        self.document_warnings = []
        self.texts = {}
        for table in ("documents", "contracts", "news"):
            for row in self.tables.get(table, []):
                self.texts[row["id"]] = self.document_text(row)

    @staticmethod
    def find_release(root, explicit=None):
        selected = explicit or os.environ.get("ARENA_RELEASE")
        candidates = [Path(selected).expanduser().resolve()] if selected else [
            root / "release-sunday", root.parent / "release-sunday"]
        for path in candidates:
            if (path / "facts.json").is_file():
                return path.resolve()
        if selected:
            raise FileNotFoundError(f"release sans facts.json : {selected}")
        return None

    def read_csv(self, path):
        fields = {f["name"]: f["type"] for f in self.resources.get(path.stem, {}).get("schema", {}).get("fields", [])}
        with path.open(encoding="utf-8", newline="") as stream:
            return [{k: _cell(v, fields.get(k, "string")) for k, v in row.items()}
                    for row in csv.DictReader(stream)]

    def rows(self, table, column=None, value=None):
        if column is None:
            return self.tables.get(table, [])
        return self.by.get((table, column), {}).get(value, [])

    def supplier_rows(self, table, sid):
        aliases = [sid] + [old for old, new in self.aliases.items() if new == sid]
        return [r for alias in aliases for r in self.rows(table, "supplier_id", alias)]

    def document_text(self, row):
        for column in ("body", "text", "content"):
            if row.get(column):
                return row[column]
        base = Path(row.get("_root", str(self.root))).resolve()
        file = row.get("file") or row.get("path")
        if not file and str(row.get("id", "")).startswith("doc:"):
            file = "correspondence/" + row["id"].split(":", 1)[1] + ".md"
        if not file:
            return ""
        path = (base / file).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"document hors package : {row['id']}")
        if not path.is_file():
            self.document_warnings.append(f"texte manquant : {row['id']}")
            return ""
        return path.read_text(encoding="utf-8")

    def fact(self, ref):
        if ref not in self.facts:
            raise KeyError(f"ID non citable : {ref}")
        return {"id": ref, "values": copy.deepcopy(self.facts[ref])}

    def verified(self, ref, field):
        return {"kind": "verified", "id": ref, "field": field,
                "value": copy.deepcopy(self.facts[ref][field]), "citable": True}

    def evidence(self, row, table):
        ref = row.get("id")
        return {"ref": ref if ref in self.facts else None,
                "source": {"table": table, "key": ref or row.get("event_id")},
                "text": self.texts.get(ref, row.get("remarks", "")),
                "text_status": "contexte documentaire ; métadonnées seulement vérifiées"}

    def context_row(self, table, row):
        key = row.get("id") or row.get("event_id") or {
            k: row.get(k) for k in ("supplier_id", "part_id", "fiscal_year", "series_id", "valid_from") if k in row}
        return {k: context(v, table, key, k) for k, v in row.items() if not k.startswith("_")}

    def dossier(self, supplier_id, part_id=None, as_of=None):
        requested = str(supplier_id)
        if not requested.startswith("sup:"):
            requested = "sup:" + requested
        sid = self.aliases.get(requested, requested)
        master = self.ids.get(sid)
        if not master or master[0] != "suppliers":
            raise KeyError(f"fournisseur inconnu : {supplier_id}")
        if part_id is not None and not str(part_id).startswith("part:"):
            part_id = "part:" + str(part_id)
        cutoff = as_of or self.as_of
        date.fromisoformat(cutoff)
        docs = [r for r in self.supplier_rows("documents", sid) if (r.get("date") or "") <= cutoff]
        news = [r for r in self.supplier_rows("news", sid) if (r.get("date") or "") <= cutoff]
        quotes = [r for r in self.supplier_rows("quotes", sid) if (r.get("quote_date") or "") <= cutoff]
        related = set()
        for table in ("sourcing", "price_conditions", "quotes", "po_lines", "should_cost"):
            related.update(r["part_id"] for r in self.supplier_rows(table, sid) if r.get("part_id"))
        for row in docs:
            related.update(re.findall(r"part:[\w-]+", row.get("part_ids") or ""))
        invitations = [r for r in self.rows("documents")
                       if r.get("kind") == "rfq" and r.get("date", "") <= cutoff
                       and master[1]["name"] in self.texts.get(r["id"], "")]
        for row in invitations:
            related.update(re.findall(r"part:[\w-]+", row.get("part_ids") or ""))
        if part_id and part_id not in related:
            raise ValueError(f"pièce sans lien documenté avec {sid} : {part_id}")
        selected_parts = [part_id] if part_id else sorted(related)
        intel_rows = self.supplier_rows("supplier_intel", sid)
        intel_row = intel_rows[0] if intel_rows else {}
        scorecards = sorted((r for r in self.supplier_rows("scorecards", sid)
                             if r["period_end"] <= cutoff), key=lambda r: r["period_end"])
        contracts = [r for r in self.supplier_rows("contracts", sid) if r["valid_from"] <= cutoff]
        clauses = [cl for ctr in contracts for cl in self.rows("contract_clauses", "contract_id", ctr["id"])]
        signals = detect_signals(self, docs, news, quotes, scorecards)
        prices = {pid: self.fair_price(sid, pid, cutoff) for pid in selected_parts}
        history = [r for r in self.supplier_rows("negotiation_history", sid)
                   if r["date"] <= cutoff and r["part_id"] in selected_parts]
        negotiations = []
        for row in sorted(history, key=lambda r: (r["date"], r["part_id"])):
            conditions = [p for p in self.rows("price_conditions", "part_id", row["part_id"])
                          if p["event_id"] == row["event_id"]]
            corroboration = []
            for cond in conditions:
                matching = self.matching_po(cond, cutoff)
                if matching:
                    corroboration.append(self.verified(matching["id"], "unit_price"))
            negotiations.append({"event": self.context_row("negotiation_history", row),
                                 "settled_price_evidence": corroboration,
                                 "tradeoff_known": bool(row.get("note")),
                                 "caution": "Le motif des concessions n'est pas documenté hors note explicite."})
        refs = {r["id"] for r in docs + news + quotes + scorecards + contracts + clauses + invitations if r["id"] in self.facts}
        quality_events = sorted((r for r in self.supplier_rows("quality_notifications", sid)
                                 if r["qn_date"] <= cutoff and r["supplier_caused"]),
                                key=lambda r: r["qn_date"], reverse=True)[:8]
        refs.update(r["id"] for r in quality_events if r["id"] in self.facts)
        for pid in selected_parts:
            refs.update(r["id"] for r in self.rows("forecasts", "part_id", pid)
                        if r["id"] in self.facts and r["snapshot_month"] <= cutoff[:7])
            refs.update(r["id"] for r in self.rows("should_cost", "part_id", pid)
                        if r["id"] in self.facts and r["estimate_date"] <= cutoff
                        and (r.get("supplier_id") is None or r["supplier_id"] == sid))
        def collect(value):
            if isinstance(value, dict):
                if value.get("kind") == "verified":
                    refs.add(value["id"])
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(prices)
        collect(negotiations)
        notices = []
        active = [c for c in contracts if c["valid_to"] >= cutoff]
        for doc in docs:
            fact = self.facts.get(doc["id"], {})
            if fact.get("days") is not None and fact.get("effective_date"):
                for ctr in active:
                    if self.facts.get(ctr["id"], {}).get("days") is not None:
                        a, b = self.verified(doc["id"], "days"), self.verified(ctr["id"], "days")
                        notices.append({"letter": a, "contract": b,
                                        "shortfall_days": derived(max(0, b["value"] - a["value"]), "max(0, préavis convenu - préavis donné)", [a, b]),
                                        "caution": "Comparer aussi prix fixes par année et préavis avant fin d'année ; aucun verdict juridique automatique."})
        payments = self.payment_history(sid, cutoff)
        dependency = self.dependency(sid, intel_row, cutoff)
        parts = {pid: {"context": self.context_row("parts", self.ids[pid][1]),
                       "sourcing": [self.context_row("sourcing", r) for r in self.rows("sourcing", "part_id", pid)],
                       "benchmark_quotes": [{"fact": self.fact(r["id"]),
                                              "missing_fields": r.get("missing_fields"),
                                              "sourcing_status": next((s["status"] for s in self.rows("sourcing", "part_id", pid)
                                                                      if s["supplier_id"] == r["supplier_id"]), None),
                                              "claim_eligibility": "À confirmer dans refs/alternative du cas ; qualification, date et conditions à vérifier."}
                                             for r in sorted(self.rows("quotes", "part_id", pid), key=lambda r: r["quote_date"], reverse=True)
                                             if r["supplier_id"] != sid and r["quote_date"] <= cutoff and r["id"] in self.facts],
                       "forecasts": [self.fact(r["id"]) for r in self.rows("forecasts", "part_id", pid)
                                     if r["id"] in self.facts and r["snapshot_month"] <= cutoff[:7]]}
                 for pid in selected_parts if pid in self.ids}
        for pid, info in parts.items():
            baseline = prices[pid].get("baseline") or {}
            current = baseline.get("price", {}).get("value")
            unit = baseline.get("price_unit", {}).get("value")
            orders = sorted((r for r in self.supplier_rows("po_lines", sid)
                             if r["part_id"] == pid and r["order_date"] <= cutoff
                             and r["order_date"] >= baseline.get("valid_from", cutoff)
                             and r.get("price_note") != "stale_condition"
                             and self.facts.get(r["id"], {}).get("unit_price") == current
                             and self.facts.get(r["id"], {}).get("price_unit") == unit),
                            key=lambda r: (r["order_date"], r["id"]), reverse=True)[:8]
            info["current_order_prices"] = [self.fact(r["id"]) for r in orders]
            refs.update(r["id"] for r in orders)
        return {"supplier_id": sid, "requested_supplier_id": requested, "as_of": cutoff,
                "coverage": "full" if intel_rows else "spend_only",
                "identity": {k: master[1].get(k) for k in ("name", "country", "category", "detail")},
                "verified_facts": [self.fact(ref) for ref in sorted(refs)],
                "context": {"supplier": self.context_row("suppliers", master[1]),
                            "financial": self.context_row("supplier_intel", intel_row),
                            "cash_balance": None, "company_margin": None,
                            "financial_caution": "Comptes déposés historiques ; aucun solde de cash ni marge réelle de société disponible.",
                            "contracts": [self.context_row("contracts", r) for r in contracts],
                            "clauses": [self.evidence(r, "contract_clauses") | {"text": r["text"]} for r in clauses],
                            "documents": [self.evidence(r, "documents") for r in docs],
                            "rfq_invitations": [self.evidence(r, "documents") for r in invitations],
                            "news": [self.evidence(r, "news") for r in news],
                            "quote_remarks": [self.evidence(r, "quotes") | {"missing_fields": r.get("missing_fields")}
                                              for r in quotes if r.get("remarks") or r.get("missing_fields")]},
                "parts": parts, "dependency": dependency, "payment_history": payments,
                "negotiation_history": negotiations,
                "performance": {"latest": self.fact(scorecards[-1]["id"]) if scorecards else None,
                                "history": [self.fact(r["id"]) for r in scorecards],
                                "recent_supplier_caused_complaints": [self.fact(r["id"]) for r in quality_events if r["id"] in self.facts],
                                "latest_context": self.context_row("scorecards", scorecards[-1]) if scorecards else {},
                                "method": "OTD sur date demandée, réceptions partielles séparées, PPM glissant ; trimestres chevauchants."},
                "signals": signals, "levers": recommend_levers(signals),
                "notice_checks": notices, "fair_price": prices,
                "warnings": list(self.document_warnings) + [
                    "Les références sup/part/EV/inv ne sont pas des IDs facts.json.",
                    "Les devis rivaux ne sont citables comme concurrence que si le cas les autorise ; vérifier qualification et termes complets.",
                    "Le prix indexé est une estimation, pas un plancher ni une cible automatique.",
                    "Comparer les term sheets avec arena_kit.total_cost et les must-haves du cas." ]}

    def matching_po(self, condition, cutoff):
        matches = [r for r in self.rows("po_lines", "part_id", condition["part_id"])
                   if self.aliases.get(r["supplier_id"], r["supplier_id"]) == self.aliases.get(condition["supplier_id"], condition["supplier_id"])
                   and condition["valid_from"] <= r["order_date"] <= cutoff
                   and (not condition.get("valid_to") or r["order_date"] <= condition["valid_to"])
                   and r.get("price_note") != "stale_condition" and r["id"] in self.facts
                   and self.facts[r["id"]].get("unit_price") == condition["price"]
                   and self.facts[r["id"]].get("price_unit") == condition["price_unit"]]
        return max(matches, key=lambda r: (r["order_date"], r["id"])) if matches else None

    def fair_price(self, sid, pid, cutoff):
        conditions = [r for r in self.rows("price_conditions", "part_id", pid)
                      if self.aliases.get(r["supplier_id"], r["supplier_id"]) == sid
                      and r["valid_from"] <= cutoff and (not r.get("valid_to") or r["valid_to"] >= cutoff)]
        cond = max(conditions, key=lambda r: r["valid_from"]) if conditions else None
        costs = [r for r in self.rows("should_cost", "part_id", pid) if r["estimate_date"] <= cutoff
                 and (r.get("supplier_id") is None or r["supplier_id"] == sid)]
        costs.sort(key=lambda r: (r.get("supplier_id") == sid, r["estimate_date"]), reverse=True)
        cost = costs[0] if costs else None
        result = {"part_id": pid, "status": "ok" if cond else "no_current_price",
                  "baseline": None, "components": [], "index_adjusted_price": None,
                  "should_cost": None, "comparison": None,
                  "cautions": ["La part de coût non indexée est maintenue fixe ; les poids ne sont pas renormalisés.",
                               "Chaque série utilise son dernier mois publié ; l'horizon peut différer entre composantes."]}
        if cost:
            value = self.verified(cost["id"], "unit_cost")
            error = self.verified(cost["id"], "confidence_pct")
            country = self.ids.get(sid, (None, {}))[1].get("country")
            applicable = bool(cost.get("supplier_id") == sid or cost["cost_base"] == country)
            result["should_cost"] = {"estimate": value, "error_pct": error,
                                     "price_unit": self.verified(cost["id"], "price_unit"),
                                     "supplier_specific": cost.get("supplier_id") == sid,
                                     "applicable_without_country_adjustment": applicable,
                                     "cost_base": cost["cost_base"], "estimate_date": cost["estimate_date"],
                                     "interval": derived([value["value"] * (1 - error["value"] / 100),
                                                          value["value"] * (1 + error["value"] / 100)],
                                                         "coût × (1 ± erreur_pct/100)", [value, error]),
                                     "caution": "Coût complet sans marge ; erreur indicative, pas intervalle statistique. Pas de facteur pays appliqué automatiquement."}
        if not cond:
            result["cautions"].append("Nouvelle pièce ou source sans condition courante : pas de prix de référence inventé.")
            return result
        po = self.matching_po(cond, cutoff)
        price = self.verified(po["id"], "unit_price") if po else context(cond["price"], "price_conditions", {
            "part_id": pid, "supplier_id": sid, "valid_from": cond["valid_from"]}, "price")
        unit = self.verified(po["id"], "price_unit") if po else context(cond["price_unit"], "price_conditions", {
            "part_id": pid, "supplier_id": sid, "valid_from": cond["valid_from"]}, "price_unit")
        result["baseline"] = {"price": price, "price_unit": unit, "valid_from": cond["valid_from"],
                              "source": self.context_row("price_conditions", cond)}
        drivers = self.rows("cost_drivers", "part_id", pid)
        weights = [context(r["weight"], "cost_drivers", {"part_id": pid, "series_id": r["series_id"]}, "weight") for r in drivers]
        total_weight = sum(w["value"] for w in weights)
        result["indexed_share"] = derived(total_weight, "somme des poids", weights)
        missing = False
        for row, weight in zip(drivers, weights):
            series = row["series_id"]
            monthly = self.index_facts.get(series, {})
            available = sorted(m for m in monthly if m <= cutoff[:7])
            start = cond["valid_from"][:7]
            end = available[-1] if available else None
            publication_lag = bool(end and end < start)
            if publication_lag:
                # Ne pas inventer la valeur du mois de prise d'effet.
                start = end
                result["status"] = "ok_with_publication_lag"
                result["cautions"].append(f"{series} : dernier indice {end} antérieur à la condition ; mouvement observé nul, évolution ultérieure inconnue.")
            if start not in monthly or end is None or end < start:
                missing = True
                result["components"].append({"series": series, "weight": weight, "status": "missing_index_window"})
                continue
            a, b = self.verified(monthly[start], "value"), self.verified(monthly[end], "value")
            if a["value"] <= 0:
                missing = True
                result["components"].append({"series": series, "weight": weight, "status": "nonpositive_base"})
                continue
            change = derived((b["value"] / a["value"] - 1) * 100, "(indice_final/indice_base - 1) × 100", [a, b])
            component = {"series": series, "weight": weight, "base_month": start, "end_month": end,
                         "publication_lag_at_baseline": publication_lag,
                         "change_pct": change,
                         "weighted_change": derived(weight["value"] * (b["value"] / a["value"] - 1),
                                                    "poids × (indice_final/indice_base - 1)", [weight, a, b]),
                         "annual_claim": None}
            annual_start = month_shift(end, -12)
            if annual_start in monthly and end >= month_shift(cutoff[:7], -12):
                aa = self.verified(monthly[annual_start], "value")
                if aa["value"] > 0:
                    ach = derived((b["value"] / aa["value"] - 1) * 100,
                                  "(indice_final/indice_annuel - 1) × 100", [aa, b])
                    component["annual_claim"] = {"refs": [aa["id"], b["id"]], "change_pct": ach,
                                                 "sentence": f"{series} : {ach['value']:+.1f} % entre {annual_start} et {end} ({aa['id']}, {b['id']}).",
                                                 "caution": "Valide temporellement ; pertinence et effet sur ce fournisseur à confirmer."}
            result["components"].append(component)
        if not drivers or missing or total_weight > 1 + 1e-9 or any(w["value"] < 0 for w in weights):
            result["status"] = "incomplete_or_invalid_drivers"
            return result
        changes = [r["weighted_change"] for r in result["components"]]
        adjusted = price["value"] * (1 + sum(c["value"] for c in changes))
        result["index_adjusted_price"] = derived(adjusted, "prix_courant × (1 + Σ poids_i × (I_final_i/I_base_i - 1))", [price] + changes)
        if cost and result["should_cost"]["applicable_without_country_adjustment"]:
            cost_unit = cost["price_unit"]
            if cost_unit != unit["value"]:
                result["cautions"].append("Unités prix/coût différentes : comparaison désactivée.")
                return result
            value = result["should_cost"]["estimate"]
            interval = result["should_cost"]["interval"]["value"]
            margin = (price["value"] - value["value"]) / price["value"] * 100 if price["value"] else None
            divergence = adjusted < interval[0]
            result["comparison"] = {"below_cost_interval": divergence,
                                    "below_cost_central": adjusted < value["value"],
                                    "gap_to_cost_pct": derived((adjusted / value["value"] - 1) * 100,
                                                               "(prix_indexé/coût - 1) × 100", [result["index_adjusted_price"], value]),
                                    "nominal_margin_pct": derived(margin, "(prix_courant - coût)/prix_courant × 100", [price, value]),
                                    "interpretation": "Coût central au-dessus du prix : examiner cash, fret et coûts réels avant une baisse." if adjusted < value["value"] else
                                                      "Prix au-dessus du coût sans marge : écart compatible avec marge, risque et conditions ; pas preuve de surprix.",
                                    "action": "Demander décomposition ; comparer C, paiement rapide et marge soutenable ; ne pas prendre le prix indexé comme cible mécanique."}
        return result

    def dependency(self, sid, intel_row, cutoff):
        completed_year = cutoff[:4] if cutoff[5:] >= "09-30" else str(int(cutoff[:4]) - 1)
        eligible = [r for r in self.rows("spend_cube") if r["fiscal_year"] <= "FY" + completed_year]
        fy = max((r["fiscal_year"] for r in eligible), default=None)
        all_rows = [r for r in eligible if r["fiscal_year"] == fy]
        own = [r for r in self.supplier_rows("spend_cube", sid) if r["fiscal_year"] == fy]
        inputs = [context(r["spend_eur"], "spend_cube", {"supplier_id": r["supplier_id"], "fiscal_year": fy}, "spend_eur") for r in own]
        spend = sum(x["value"] for x in inputs)
        total = sum(r["spend_eur"] for r in all_rows)
        result = {"fiscal_year": fy, "spend_eur": derived(spend, "somme dépenses fournisseur + anciens numéros", inputs),
                  "share_of_buyer_spend_pct": derived(spend / total * 100 if total else None,
                                                     "dépense fournisseur / dépense totale spend_cube × 100", inputs + [context(total, "spend_cube", {"fiscal_year": fy}, "SUM(spend_eur)")]),
                  "share_of_supplier_turnover_pct": None,
                  "caution": "Achats de l'exercice récent comparés à un CA déposé antérieur ; achats ≠ CA fournisseur exact, TVA et période à vérifier."}
        if intel_row.get("turnover_eur"):
            revenue = context(intel_row["turnover_eur"], "supplier_intel", sid, "turnover_eur")
            result["share_of_supplier_turnover_pct"] = derived(spend / revenue["value"] * 100,
                                                               "achats / CA déposé × 100", inputs + [revenue])
        else:
            band = intel_row.get("turnover_band") or ""
            match = re.fullmatch(r"(\d+)-(\d+) M EUR", band)
            if match:
                lo, hi = (int(v) * 1000000 for v in match.groups())
                result["share_of_supplier_turnover_pct"] = derived([spend / hi * 100, spend / lo * 100 if lo else None],
                    "achats/borne_sup_CA à achats/borne_inf_CA ; borne haute inconnue si CA_min nul",
                    inputs + [context(band, "supplier_intel", sid, "turnover_band")])
        return result

    def payment_history(self, sid, cutoff):
        invoices = [r for r in self.supplier_rows("invoices", sid)
                    if r["invoice_date"] <= cutoff and r.get("paid_date") and r["paid_date"] <= cutoff and not r.get("discount_taken")]
        rows = []
        for r in invoices:
            lag = (date.fromisoformat(r["paid_date"]) - date.fromisoformat(r["due_date"])).days
            rows.append((r, lag))
        inputs = [context(r["paid_date"], "invoices", r["id"], "paid_date") for r, _ in rows]
        inputs += [context(r["due_date"], "invoices", r["id"], "due_date") for r, _ in rows]
        return {"mean_days_vs_due": derived(sum(lag for _, lag in rows) / len(rows) if rows else None,
                                           "moyenne(paid_date - due_date), factures réglées sans escompte", inputs),
                "caution": "Retard de Hollerau, pas retard du fournisseur ; valeurs hors facts.json."}


# Chaque motif explique son levier ; aucune classification par ID de fournisseur.
SIGNAL_RULES = {
    "cash_sensitive": (r"pay.*(?:earlier|within 10 days)|bank is watching|cash and the banks|payment within 10 days requested|credit_watch|credit_downgrade|restructuring|credit agency has lowered", "probable", "Le cash et/ou les banques pèsent ; sensibilité au paiement probable."),
    "margin_protection": (r"cannot sign anything that lowers our margin|loss-making", "probable", "La protection de marge est explicitement invoquée."),
    "free_capacity": (r"shop floor was idle|second shift has been on hold|free machine capacity|short-time work|additional parts at short notice|new automatic moulding line", "probable", "Capacité disponible probable ; son coût marginal reste inconnu."),
    "capacity_tight": (r"very high capacity utilisation|binding 12-month commitment|full_order_book|order backlog|most of its turning capacity|allocated in line|chip_shortage|shortages of power semiconductors", "probable", "Capacité contrainte/allocation ; engagement ferme probablement utile."),
    "outdated_cost_view": (r"spreadsheet, last updated two years ago|not followed his material and energy prices closely", "probable", "Chiffrage ancien déclaré ; indices récents susceptibles d'éclairer la discussion."),
    "hedged_inputs": (r"bought (?:their|our) .* forward|covered at fixed prices|material covered until|electricity supply.*long-term contract", "probable", "Achats couverts ; transmission du spot différée."),
    "price_frozen": (r"frozen all price reductions|price list applies to all customers without exception|discounts not possible this year", "probable", "Gel tarifaire déclaré ; explorer les conditions non tarifaires."),
    "approval_ladder": (r"pricing committee|approval of their", "probable", "Concessions tarifaires soumises à une approbation interne."),
    "account_exit": (r"not strategic|individual agreements will not be extended|refocus|concentrate on larger key accounts|list conditions; supply from stock only", "probable", "Désengagement des petits comptes ; risque de non-renouvellement."),
    "volume_focus": (r"focus on customers with volume|only multi-year volume agreements change prices|expected volumes", "probable", "Volumes/durée à explorer, sans supposer que le gel de prix peut être levé."),
    "relationship_memory": (r"expects partnership in return|important reference customer", "probable", "Réciprocité ou valeur de référence commerciale déclarée."),
    "open_costing": (r"walk us through their calculation", "probable", "Ouverture déclarée à expliquer le calcul de coût."),
    "buyer_late_payment": (r"invoices are overdue again", "prouvé", "Le fournisseur reproche des retards de paiement à Hollerau."),
}


def detect_signals(package, docs, news, quotes, scorecards):
    sources = [("documents", r, package.texts.get(r["id"], "")) for r in docs]
    sources += [("news", r, (r.get("topic") or "") + " " + package.texts.get(r["id"], "")) for r in news]
    sources += [("quotes", r, r.get("remarks") or "") for r in quotes]
    signals = []
    for name, (pattern, confidence, explanation) in SIGNAL_RULES.items():
        evidence = []
        for table, row, text in sources:
            if re.search(pattern, text, re.I):
                evidence.append(package.evidence(row, table) | {"text": text})
        if evidence:
            signals.append({"type": name, "confidence": confidence, "explanation": explanation,
                            "evidence": evidence})
    if scorecards and scorecards[-1]["colour"] == "red":
        signals.append({"type": "quality_delivery_red", "confidence": "prouvé",
                        "explanation": "Scorecard récente rouge ; distinguer qualité et délai demandé.",
                        "evidence": [package.fact(scorecards[-1]["id"])]})
    kinds = {s["type"] for s in signals}
    if {"free_capacity", "capacity_tight"} <= kinds:
        signals.append({"type": "conflicting_capacity", "confidence": "hypothèse",
                        "explanation": "Signaux de capacité opposés : demander situation récente par atelier et pièce.",
                        "evidence": [e for s in signals if s["type"] in {"free_capacity", "capacity_tight"} for e in s["evidence"]]})
    return signals


LEVER_RULES = {
    "cash_sensitive": ("Comparer paiement rapide contre baisse chiffrée, avec le coût du capital acheteur.", "Allonger les délais ou exiger une baisse sans contrepartie."),
    "margin_protection": ("Protéger une marge estimée soutenable ; proposer cash, volume réaliste et durée.", "Assimiler indice en baisse à une marge réelle disponible."),
    "free_capacity": ("Offrir charge ferme ou regroupement de pièces contre prix et livraison.", "Promettre un volume hors besoin ; le surplus est acheté dans C."),
    "capacity_tight": ("Demander allocation et calendrier contre engagement ferme et durée couvrant le besoin.", "Menacer ou imposer un délai irréalisable ; vérifier qualification d'une alternative."),
    "outdated_cost_view": ("Présenter une ou deux variations d'indices pertinentes et demander une décomposition.", "Empiler des indices sans poids ou utiliser une fenêtre trop ancienne pour l'API."),
    "hedged_inputs": ("Demander date de fin de couverture et transition ; négocier base et extras.", "Appliquer la baisse spot à des achats déjà couverts ou indexer deux fois."),
    "price_frozen": ("Chiffrer paiement, incoterm, outillage, flexibilité et couverture complète dans C.", "Insister sur le prix malgré le gel ; volume/durée ne prouvent pas une exception."),
    "approval_ladder": ("Demander qui approuve et transmettre plusieurs packages avec contreparties.", "Supposer que l'interlocuteur peut valider une concession importante immédiatement."),
    "account_exit": ("Comparer l'alternative et la continuité de service ; envisager une sortie si C l'indique.", "Supposer qu'il veut conserver le compte ; révéler l'absence d'alternative."),
    "volume_focus": ("Proposer durée et volume compatibles avec le besoin, comparer le coût non couvert.", "Conclure trop court ou suracheter pour une réduction de prix."),
    "relationship_memory": ("Reconnaître les engagements passés et proposer une contrepartie concrète.", "Traiter l'historique comme un droit automatique à une nouvelle remise."),
    "open_costing": ("Demander le détail matière/conversion et les taux avant de choisir les arguments.", "Confondre modèle should-cost allemand et coût exact de l'usine."),
    "buyer_late_payment": ("Corriger la crédibilité de paiement et comparer un package de règlement fiable.", "Présenter nos retards comme une faute fournisseur ou promettre sans engagement."),
    "quality_delivery_red": ("Demander plan 8D, PPM réaliste et chargebacks ; chiffrer qualité dans C.", "Moyenner plusieurs trimestres glissants comme observations indépendantes."),
    "conflicting_capacity": ("Faire préciser atelier, date et pièce avant de négocier la charge.", "Choisir arbitrairement le signal le plus favorable."),
}


def recommend_levers(signals):
    return [{"signal": s["type"], "confidence": s["confidence"],
             "play": LEVER_RULES[s["type"]][0], "avoid": LEVER_RULES[s["type"]][1],
             "refs": sorted({e.get("ref") or e.get("id") for e in s["evidence"] if e.get("ref") or e.get("id")})}
            for s in signals if s["type"] in LEVER_RULES]


def _fingerprint(root, release):
    paths = [root / "facts.json", root / "world.sqlite", root / "datapackage.json", root / "KNOWN_ISSUES.md"]
    for base in [root] + ([release] if release else []):
        paths.extend(base.glob("data/*.csv"))
        paths.extend(base.glob("correspondence/*.md"))
        paths.extend(base.glob("contracts/*.md"))
        if base != root:
            paths.append(base / "facts.json")
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(set(paths)) if p.is_file())


@lru_cache(maxsize=4)
def _cached_package(root, release, fingerprint):
    return Package(root, release)


def clear_cache():
    _cached_package.cache_clear()


def supplier_dossier(package_dir, supplier_id, part_id=None, *, release_dir=None, as_of=None):
    """Dossier JSON sérialisable ; cache invalidé quand le package/release change.

    `verified_facts` et mesures `kind=verified` portent les valeurs exactes
    de facts.json. `context` et `kind=derived` ne sont pas des citations brutes.
    """
    root = Path(package_dir).expanduser().resolve()
    release = Package.find_release(root, release_dir)
    pkg = _cached_package(str(root), str(release) if release else None, _fingerprint(root, release))
    return pkg.dossier(supplier_id, part_id, as_of=as_of)


def agent_brief(dossier, part_id=None, *, case=None, used_refs=(), max_claims=None):
    """Vue compacte pour le prompt de Claude ; conserver le dossier pour détail.

    Ne pas mettre tous les IDs du dossier dans les refs d'un message.
    """
    prices = dossier["fair_price"]
    if part_id:
        prices = {part_id: prices[part_id]}
    fair = {}
    for pid, price in prices.items():
        fair[pid] = {k: price[k] for k in ("status", "index_adjusted_price", "should_cost", "comparison")}
        fair[pid]["baseline"] = {k: v for k, v in (price.get("baseline") or {}).items() if k != "source"}
        fair[pid]["index_arguments"] = [c["annual_claim"] for c in price["components"] if c.get("annual_claim")]
        if case and case["buyer"].get("category_goal") not in {"stabilise", "secure_supply"} and fair[pid]["comparison"]:
            fair[pid]["comparison"] = {k: v for k, v in fair[pid]["comparison"].items()
                                       if k != "nominal_margin_pct"}
            fair[pid]["comparison"]["action"] = "Comparer prix, coûts et conditions dans C ; aucun modèle de marge/cash fournisseur pour cet objectif."
    return {"supplier_id": dossier["supplier_id"], "identity": dossier["identity"],
            "as_of": dossier["as_of"], "performance": dossier["performance"]["latest"],
            "levers": dossier["levers"], "fair_price": fair,
            "notice_checks": dossier["notice_checks"],
            "dependency": dossier["dependency"],
            "benchmark_quotes": {pid: dossier["parts"].get(pid, {}).get("benchmark_quotes", [])[:3] for pid in prices},
            "citable_claims": citable_claims(dossier, part_id, case=case, used_refs=used_refs, limit=max_claims),
            "warnings": dossier["warnings"]}


def citable_claims(dossier, part_id=None, *, case=None, used_refs=(), limit=None):
    """Candidats conformes aux règles publiées ; sélectionner au plus trois/message.

    `kind` utilise les types de l'arène ; `asserted_value` reste scalaire.
    La vérification locale ne simule pas le lecteur linguistique de l'API.
    """
    if limit is not None and not 0 <= limit <= 3:
        raise ValueError("maximum trois claims par message")
    parts = [part_id] if part_id else list(dossier["fair_price"])
    candidates = []
    def add(priority, kind, refs, value, sentence):
        if not set(refs).intersection(used_refs):
            candidates.append((priority, {"sentence_en": sentence, "refs": refs,
                                         "asserted_value": value, "kind": kind}))
    for notice in dossier["notice_checks"]:
        a, b = notice["letter"], notice["contract"]
        add(0 if a["value"] < b["value"] else 30, "notice_period", [a["id"], b["id"]],
            a["value"], f"The notice letter gives {a['value']} days of notice against the referenced contract.")
    card = dossier["performance"]["latest"]
    quality = case is not None and case["buyer"].get("category_goal") == "quality_first"
    if card:
        for field, kind, unit, title in [("ppm", "quality_record", "ppm", "quality record"),
                                         ("otd_pct", "delivery_record", "%", "on-time delivery rate")]:
            value = card["values"].get(field)
            if value is not None:
                add(1 if quality and field == "ppm" else 8 if field == "otd_pct" else 12,
                    kind, [card["id"]], value, f"Your recorded {title} is {value:g} {unit}.")
    allowed_quotes = set((case or {}).get("refs", []))
    alternative = (case or {}).get("buyer", {}).get("alternative") or {}
    if alternative.get("ref"):
        allowed_quotes.add(alternative["ref"])
    for pid in parts:
        info = dossier["parts"].get(pid, {})
        forecasts = info.get("forecasts", [])
        if forecasts:
            f = max(forecasts, key=lambda r: r["id"])
            value = f["values"].get("change_pct")
            if value is not None:
                add(6, "volume_change", [f["id"]], value,
                    f"The recorded demand forecast for {pid} changed by {value:+g}%.")
        orders = info.get("current_order_prices", [])
        if len(orders) >= 2:
            value = orders[0]["values"]["unit_price"]
            add(15, "order_history", [r["id"] for r in orders], value,
                f"The referenced orders for {pid} each record a price of EUR {value:g} per price unit.")
        price = dossier["fair_price"][pid]
        cost = price.get("should_cost")
        if cost and cost.get("applicable_without_country_adjustment"):
            f = cost["estimate"]
            add(18, "should_cost", [f["id"]], f["value"],
                f"Our should-cost estimate for {pid} is EUR {f['value']:g} per price unit.")
        for component in price["components"]:
            claim = component.get("annual_claim")
            if claim:
                value = claim["change_pct"]["value"]
                add(10 if value < 0 else 20, "index_change", claim["refs"], value,
                    f"{component['series']} changed by {value:+.4f}% over the referenced annual window.")
        for quote in info.get("benchmark_quotes", []):
            f = quote["fact"]
            if f["id"] in allowed_quotes and f["values"].get("unit_price") is not None:
                value = f["values"]["unit_price"]
                add(25, "rival_quote", [f["id"]], value,
                    f"The alternative supplier's quote for {pid} is EUR {value:g} per price unit.")
    unique = {}
    for priority, claim in sorted(candidates, key=lambda x: (x[0], x[1]["refs"])):
        key = tuple(claim["refs"]), claim["kind"]
        unique.setdefault(key, claim)
    return select_claims(list(unique.values()), limit=limit, used_refs=used_refs)


def select_claims(claims, *, limit=3, used_refs=()):
    """Sélection pour un message : pas de réutilisation du même ID entre arguments.

    limit=None renvoie le catalogue d'alternatives ; ne pas l'envoyer intégralement.
    """
    if limit is None:
        return [c for c in claims if not set(c["refs"]).intersection(used_refs)]
    if not 0 <= limit <= 3:
        raise ValueError("maximum trois claims par message")
    selected, used = [], set(used_refs)
    for claim in claims:
        if len(selected) >= limit:
            break
        if not used.intersection(claim["refs"]):
            selected.append(claim)
            used.update(claim["refs"])
    return selected
