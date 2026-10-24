"""Data exceptions: what is missing or ambiguous in a message, and candidate resolutions for a human.

The bridge never fills a gap itself. A gap is a hold, and the hold carries a DataException: which field, what
is wrong, and suggestions, each with the basis it rests on and the evidence it cites. Suggestions are unverified
by construction (``verified`` is always False here); only a human's decision applies one, through the review
queue.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Suggestion:
    value: Any
    basis: str                     # why this value is a candidate
    evidence: list[str] = field(default_factory=list)   # where the basis comes from: fields, records, profile entries
    plausible: bool = True         # passes a plausibility check (for example a value inside the reportable range)
    verified: bool = False         # never True: a suggestion is a proposal, not a fact


@dataclass
class DataException:
    exception_id: str
    field: str                     # the field that is missing or ambiguous, for example specimen_id or unit
    issue: str                     # missing_accession, unit_mismatch, ...
    detail: str
    suggestions: list[Suggestion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
