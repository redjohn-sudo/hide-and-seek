"""Optional LLM narration: the code fixes every number, a model writes the e-mail around them.

OG-Narrator (Xia et al., ACL 2024): a deterministic offer generator plus an LLM narrator raised a
buyer's deal rate from 27 % to 89 %. Here the model receives our term sheets, the verified
argument sentences (to keep word for word) and the supplier's last message, and writes a short,
warm and firm reply to what the supplier actually said. The guard then checks the result like any
other message; on any failure, timeout or doubt we keep the template body.
"""

import logging
import os
import re
import time

log = logging.getLogger("pegasus")

ENABLED = os.environ.get("PEGASUS_LLM_PROSE", "0") == "1"
BUDGET_S = float(os.environ.get("PEGASUS_LLM_BUDGET_S", "20"))

SYSTEM = """You write the buyer's next e-mail in a B2B procurement negotiation (buyer: Hollerau
Pumpenwerke GmbH, Purchasing). The numbers are decided; you only write the words.
Rules:
- Reply to what the supplier said in its last e-mail (its reasons, questions, constraints), warmly
  and respectfully, then hold our position firmly. At most 150 words.
- Copy every line listed under ARGUMENTS word for word, each on its own line (they carry data IDs
  that are checked). Do not add any other number, percentage, price, date or data ID.
- Do not restate the term sheets' figures; say the packages are attached.
- Never mention an alternative supplier unless an ARGUMENT does, a budget, urgency, deadlines,
  or that we have no alternative. No threats. No placeholders.
- End with: Best regards / Hollerau Pumpenwerke GmbH, Purchasing
Return only the e-mail body."""

_complete = None


def _client():
    global _complete
    if _complete is None:
        from arena_kit.agents.llm import llm_from_env

        _complete = llm_from_env()
    return _complete


def narrate(supplier_text, template_body, argument_lines, extra_lines):
    """The model's body, or None (disabled, slow, failed or not keeping the arguments)."""
    if not ENABLED:
        return None
    user = ("SUPPLIER'S LAST E-MAIL:\n" + (supplier_text or "(opening quote)")[:2500]
            + "\n\nARGUMENTS (copy word for word):\n" + "\n".join(argument_lines or ["(none)"])
            + "\n\nPOINTS TO MAKE (rephrase freely, no new numbers):\n" + "\n".join(extra_lines)
            + "\n\nOUR TEMPLATE (for reference):\n" + template_body)
    t0 = time.monotonic()
    try:
        body = (_client()(SYSTEM, user) or "").strip()
    except Exception as e:  # a slow or broken model must never cost the round
        log.warning("prose LLM failed (%s) - template kept", e)
        return None
    took = time.monotonic() - t0
    if took > BUDGET_S:
        log.warning("prose LLM took %.1fs - template kept", took)
        return None
    body = re.sub(r"^```.*?\n|```$", "", body, flags=re.S).strip()
    if not body or any(line not in body for line in argument_lines or []):
        log.warning("prose LLM dropped an argument - template kept")
        return None
    return body[:8000]
