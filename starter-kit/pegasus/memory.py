"""What we learnt about a supplier in earlier negotiations, reused in the next one.

Only our own sessions feed it (the rules allow that; the live feed is never read). In the final the
40 suppliers come from the data package and may reappear across waves: a supplier that refused
chargebacks, needs faster payment, insists on its lead time or requires an index clause is treated
so from the first e-mail. Stored per supplier: signal counts, the numbers it stated, its deals.
"""

import json
import os
import threading
import time
from pathlib import Path

from .reader_cache import write_atomic

FILE = Path(os.environ.get("PEGASUS_MEMORY", "data/supplier_memory.json"))
_lock, _mem = threading.Lock(), {}
PRIOR_MIN = int(os.environ.get("PEGASUS_PRIOR_MIN", "2"))  # one refusal is not a law (P11: chargeback 500 was accepted in another run)
PRIOR_FLAGS = {"wants_faster_payment": "cash", "price_frozen": "freeze", "requires_index_clause": "wants_index",
               "lead_time_firm": "lead_firm", "chargeback_too_high": "cb_too_high",
               "wants_longer_contract": "wants_duration", "wants_volume_commitment": "wants_volume"}


def _load():
    if not _mem and FILE.exists():
        try:
            _mem.update(json.loads(FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return _mem


def recall(sid):
    with _lock:
        return dict(_load().get(sid) or {})


def prior_flags(sid, case):
    """Flags to set before the first e-mail, from signals this supplier gave at least once before -
    only for levers this case lets us negotiate."""
    m = recall(sid)
    neg = set(case.get("negotiable", []))
    out, stated = {}, {}
    for sig, flag in PRIOR_FLAGS.items():
        if (m.get("signals") or {}).get(sig, 0) >= PRIOR_MIN:
            field = {"cash": "payment_days", "wants_index": "index_clause", "lead_firm": "lead_time_days",
                     "cb_too_high": "chargeback_eur_per_complaint", "wants_duration": "contract_months",
                     "wants_volume": "volume_per_year"}.get(flag)
            if field is None or field in neg:
                out[flag] = True
    for k, v in (m.get("stated") or {}).items():
        if k in ("chargeback_eur_per_complaint", "lead_time_days"):
            stated[k] = v
    return out, stated


def remember(sid, st, case, result=None):
    """Merge what this thread taught us; atomic write."""
    with _lock:
        m = _load().setdefault(sid, {"signals": {}, "stated": {}, "deals": []})
        for sig in st.get("signals_read") or []:
            m["signals"][sig] = m["signals"].get(sig, 0) + 1
        m["stated"].update({k: v for k, v in (st.get("stated") or {}).items()})
        if result and result.get("deal") and result.get("supplier_id") == sid:
            m["deals"].append({"case": case.get("case_id"), "type": case.get("type"), "parts": case.get("parts"),
                               "opening": (st.get("open") or {}).get("unit_price"), "terms": result.get("terms"),
                               "score": result.get("score"), "at": int(time.time())})
            m["deals"] = m["deals"][-20:]
        m["updated"] = int(time.time())
        try:
            write_atomic(FILE, _mem)
        except OSError:
            pass
