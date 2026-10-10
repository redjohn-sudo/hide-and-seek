"""Shared structures between the bricks. Change them only with a note in the vault (Décisions).

compile_case(case)            -> Economics      (owner: economics lane)
compile_evidence(case, data)  -> list[Claim]    (owner: economics lane)
read_reply(case, thread)      -> Observation    (owner: LLM lane)
plan(economics, belief)       -> list[Move]     (owner: integration)
strategist(board)             -> Move           (owner: LLM lane)
guard(move, case, threads)    -> Move           (owner: runner lane)
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Terms = dict[str, Any]  # a term sheet: exactly the case's negotiable fields


@dataclass
class Lever:
    field: str
    best: Any  # the allowed value that costs us least
    worst: Any
    cost_of_forgetting_eur: float  # what we lose if this lever stays at its worst value


@dataclass
class Economics:
    case_id: str
    cost: Callable[[Terms], float]  # exact C of any term sheet (kit cost function, supplier standard terms applied)
    must_haves_ok: Callable[[Terms], bool]
    levers: list[Lever]
    best_package: Terms  # non-price terms that minimise C among must-have-compatible values
    c_alt: float  # C of walking away (inf when there is no alternative)
    reference_price: float


@dataclass
class Claim:
    family: str  # index | quote | scorecard | orders | forecast_cost_news | notice
    ids: list[str]  # data IDs, all present in facts (package, Sunday release or case facts)
    figure: float | None  # the number we assert, already checked against the facts; None = cite without figure
    unit: str  # "%", "EUR", "ppm", "days", ""
    sentence: str  # one sentence, figure next to its IDs
    supports: str  # price | effective_date | payment | quality | volume | other


@dataclass
class Observation:
    supplier_id: str
    status: str  # open | final_offer | agreed | walked_away | no_bid
    standing_id: str | None
    standing: Terms
    answered_sheet: int | None  # which of our sheets it answered, if identifiable
    price_move: float | None  # change of its unit price since its previous offer
    granted: list[str] = field(default_factory=list)  # fields where it took our value
    claim_results: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)  # constraints, trades it offers, tone
    seconds: float = 0.0  # how long its reply took


@dataclass
class Move:
    supplier_id: str
    intent: str  # probe | trade | squeeze | hold | accept | walk_away
    term_sheets: list[Terms] = field(default_factory=list)  # 0-3, complete and must-have-compatible
    claims: list[Claim] = field(default_factory=list)  # at most 3
    questions: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    expected_score: float | None = None
    accept_id: str | None = None

    def to_message(self) -> dict:
        """The arena message of this move."""
        if self.intent == "walk_away":
            return {"action": {"type": "walk_away"}}
        if self.intent == "accept":
            return {"action": {"type": "accept", "term_sheet_id": self.accept_id}}
        return {"subject": self.subject, "body": self.body, "term_sheets": [{"terms": t} for t in self.term_sheets]}
