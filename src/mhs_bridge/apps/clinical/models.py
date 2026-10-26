"""Clinical application state: orders and their fulfilment lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Order:
    """An EHR order (FHIR ServiceRequest) as the clinical bridge tracks it.

    The lifecycle is mirrored to the EHR as a FHIR Task:
    requested -> accepted -> in-progress -> completed, or on-hold with a reason.
    """
    order_id: str
    patient_id: str
    specimen_id: str
    loinc_codes: list[str]
    priority: str
    task_id: str
    task_status: str = "requested"
    status_reason: str = ""
    routing: dict = field(default_factory=dict)          # device_id -> [vendor test codes]
    unroutable: list[str] = field(default_factory=list)
    report_id: str = ""
    history: list[dict] = field(default_factory=list)
    source: dict = field(default_factory=dict)            # the ServiceRequest as received
    exceptions: list[dict] = field(default_factory=list)  # unresolved data exceptions; dispatch waits for a human

    def to_dict(self) -> dict:
        return asdict(self)
