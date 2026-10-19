"""Canonical, vendor-neutral result model produced by the clinical drivers.

A clinical driver decodes its device's protocol and normalises the vendor
dialect (codes, units, flags) into these types using a vendor profile. Anything
reading the device through MHS, an agent or an application, then sees LOINC,
UCUM and canonical conditions, never vendor codes.

``release`` and ``acknowledgements`` are set by the clinical application, not by
the driver: a driver reports what the device measured; deciding what may reach
a patient record is an application concern.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class ReleaseState(str, Enum):
    HELD = "held"            # blocked by a safety or QC rule, needs action
    PENDING_ACK = "pending_ack"  # critical value, needs human acknowledgement
    RELEASABLE = "releasable"
    RELEASED = "released"


@dataclass
class CanonicalResult:
    result_id: str
    device_id: str
    specimen_id: str
    test_code: str           # LOINC
    test_display: str
    value: float | None
    unit: str                # UCUM
    ref_low: float | None
    ref_high: float | None
    interpretation: str      # N, L, H, LL, HH
    status: str              # final, preliminary
    observed_at: str
    conditions: list[str] = field(default_factory=list)   # canonical flags: foam, short_sample, clot
    vendor_raw: dict = field(default_factory=dict)
    release: ReleaseState = ReleaseState.RELEASABLE
    hold_reasons: list[str] = field(default_factory=list)
    patient_id: str = ""
    acknowledgements: list[dict] = field(default_factory=list)
    kind: str = "lab"        # lab or vital
    order_id: str = ""       # FHIR ServiceRequest id this result fulfils, if any

    def to_dict(self) -> dict:
        d = asdict(self)
        d["release"] = self.release.value
        return d


@dataclass
class QCResult:
    device_id: str
    test_code: str
    level: str
    value: float
    target_mean: float
    target_sd: float
    run_at: str

    @property
    def z(self) -> float:
        return (self.value - self.target_mean) / self.target_sd

