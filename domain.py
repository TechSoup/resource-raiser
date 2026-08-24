"""Stable records at the query engine's boundaries.

These are deliberately small. Resource Raiser keeps empirical backtracking; the records make its
interpretation, attempts, admitted facts, and final answer inspectable without inventing a plan IR.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class QueryIntent:
    question: str
    operation: str = "point"
    entity: str | None = None
    entity_type: str = "none"
    measure: str = ""
    period: str = "latest"
    entities: list[str] = field(default_factory=list)
    threshold: dict[str, Any] | None = None
    quantifier: str = "exhaustive"
    sites: list[str] = field(default_factory=list)

    @classmethod
    def from_context(cls, question: str, ctx: dict[str, Any], sites=()) -> "QueryIntent":
        return cls(question=question, operation=ctx.get("shape") or "point",
                   entity=ctx.get("entity") or None, entity_type=ctx.get("type") or "none",
                   measure=ctx.get("attribute") or "", period=ctx.get("period") or "latest",
                   entities=list(ctx.get("entities") or []), threshold=ctx.get("threshold"),
                   quantifier=ctx.get("quantifier") or "exhaustive", sites=list(sites or []))

    def to_dict(self):
        return asdict(self)


@dataclass(slots=True)
class Check:
    name: str
    status: str                         # pass | fail | inconclusive
    reason: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(slots=True)
class Validation:
    accepted: bool
    checks: list[Check] = field(default_factory=list)
    reason: str = ""
    residual_semantic_check: bool = False

    def to_dict(self):
        return {"accepted": self.accepted, "reason": self.reason,
                "residual_semantic_check": self.residual_semantic_check,
                "checks": [c.to_dict() for c in self.checks]}


@dataclass(slots=True)
class Attempt:
    source: str
    identifier: str
    entity: dict[str, Any] | None = None
    period: str = "latest"
    outcome: str = "started"            # started | rejected | accepted | error
    reason: str = ""
    validation: Validation | None = None
    request: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: int | None = None

    def to_dict(self, include_raw=False):
        out = asdict(self)
        if not include_raw:
            out.pop("raw", None)
        return out


@dataclass(slots=True)
class Evidence:
    kind: str
    source: str
    identifier: str
    payload: dict[str, Any]
    entity: dict[str, Any] | None = None
    measure: str = ""
    value: Any = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    grain: str | None = None
    period_basis: str | None = None
    entity_key: str | None = None
    quantity_kind: str | None = None
    population_complete: bool | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    transformations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass(slots=True)
class Answer:
    text: str
    renderer: str
    evidence_kind: str

    def to_dict(self):
        return asdict(self)


@dataclass(slots=True)
class ClarificationOption:
    """One human-readable way to resolve a materially ambiguous query."""
    id: str
    label: str
    value: Any = None
    unit: str | None = None
    period: str | None = None
    source: str | None = None
    concept: str | None = None
    assumptions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Source adapters sometimes cross a float-producing boundary even for exact whole-dollar
        # facts. Keep the public protocol faithful to the quantity: 96995000000 is an integer, not
        # an IEEE approximation that clients may display in scientific notation.
        if isinstance(self.value, float) and self.value.is_integer():
            self.value = int(self.value)

    def to_dict(self):
        return asdict(self)


@dataclass(slots=True)
class Clarification:
    """A terminal API outcome that a caller can resolve with a follow-up request."""
    question: str
    options: list[ClarificationOption]
    attribute: str = ""

    def to_dict(self):
        return {"question": self.question, "attribute": self.attribute,
                "options": [option.to_dict() for option in self.options]}
