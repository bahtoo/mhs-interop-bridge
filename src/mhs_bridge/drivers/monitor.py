"""MHS driver for a bedside patient monitor (HL7 v2 ORU^R01, IEEE 11073 terms).

The manifest has states and no procedures, and the profile declares
``risk_class: patient_connected``, so the runtime refuses any invoke. Data
flows out for charting; control never flows back in through MHS.
"""

from __future__ import annotations

from typing import Any

from ..mhs_shim import Manifest, State
from ..protocols import hl7
from ..sim.clinical_instruments import SimBedsideMonitor
from .dialects import Profile, normalise


class MonitorDriver:
    def __init__(self, profile: Profile, instrument: SimBedsideMonitor):
        self.profile = profile
        self.device_id = profile.device_id
        self.instrument = instrument
        self.sets: dict[str, list] = {}
        self.latest = ""
        if instrument.online:
            self.receive()          # the monitor has already pushed its first periodic set

    @property
    def online(self) -> bool:
        return self.instrument.online

    def receive(self) -> str:
        set_id, frame = self.instrument.push()
        results = normalise(self.profile, hl7.mllp_unwrap(frame))
        for r in results:
            r.kind = "vital"
            r.result_id = f"{self.device_id}-{r.result_id}"
        self.sets[set_id] = results
        self.latest = set_id
        return set_id

    def manifest(self) -> Manifest:
        return Manifest(
            self.device_id, self.profile.device_type, self.profile.data["vendor"], self.profile.data["model"],
            "HL7 v2.5.1 ORU^R01 over MLLP, IEEE 11073-10101 terms",
            [State("status", "string", "online or offline"),
             State("association", "object", "bed location and the patient this monitor is bound to"),
             State("latest_vitals", "object", "the most recent vital-signs set, normalised to LOINC and UCUM, "
                                              "with signal-quality conditions")],
            [], self.profile.tags, self.profile.data.get("risk_class", ""), driver="monitor")

    def read(self, state: str) -> Any:
        if state == "status":
            return "online" if self.online else "offline"
        if state == "association":
            return self.instrument.association()
        if state == "latest_vitals":
            return {"set_id": self.latest, "results": [r.to_dict() for r in self.sets.get(self.latest, [])]}
        raise KeyError(state)

    def write(self, procedure: str, args: dict) -> dict:     # pragma: no cover - the runtime refuses first
        raise PermissionError("patient-connected devices accept no procedures")
