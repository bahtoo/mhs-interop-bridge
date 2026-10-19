"""Simulated clinical instruments that emit real wire formats.

* ``SimAnalyzer``: an analyzer that produces ASTM E1394 or HL7 OUL^R22 result
  messages for a specimen or QC run, and accepts host orders into a worklist.
* ``SimBedsideMonitor``: a monitor that pushes periodic HL7 ORU^R01 vital-sign
  messages with IEEE 11073 (MDC) terms.

Faults are injected into the instrument's behaviour or onto the wire, never by
short-circuiting a decoder:

    analyzer: offline, line_noise_naks, nak_storm, foam/clot/short_sample (specimen ids),
              critical (specimen ids), unit_drift {code: {value, unit}}, qc_shift {test: z},
              patient_mismatch (specimen ids)
    monitor:  offline, artefact (MDC codes), lead_off (MDC codes), unassociated
"""

from __future__ import annotations

import itertools
import zlib

from ..drivers.dialects import Profile
from ..protocols import astm, hl7, lis_orders

DEFAULT_PATIENTS = {"S-1001": "P-001", "S-1002": "P-002", "H-2001": "P-003"}
NORMAL_VALUES = {"GLU": 5.2, "K": 4.2, "WBC": 7.1, "HGB": 140.0, "PLT": 250.0}
CRITICAL_VALUES = {"GLU": 33.0, "K": 6.9, "WBC": 0.9, "HGB": 55.0, "PLT": 12.0}
TS = "20261019101500"


class SimAnalyzer:
    def __init__(self, profile: Profile, faults: dict | None = None):
        self.profile = profile
        self.faults = faults or {}
        self.worklist: dict[str, dict] = {}
        self._qc_runs = 0

    @property
    def online(self) -> bool:
        return not self.faults.get("offline", False)

    # --- host to instrument -----------------------------------------------

    def receive_order(self, message: str) -> dict:
        if self.profile.protocol == "astm":
            entry = lis_orders.decode_astm_host_order(message)
        else:
            entry = lis_orders.decode_oml_o33(message)
        self.worklist[entry["specimen_id"]] = entry
        return entry

    # --- instrument to host --------------------------------------------------

    def patient_for(self, specimen: str) -> str:
        if specimen in self.faults.get("patient_mismatch", []):
            return "P-999"
        if specimen in self.worklist:
            return self.worklist[specimen]["patient_id"]
        return DEFAULT_PATIENTS.get(specimen, "")

    def _value(self, specimen: str, code: str, first: bool) -> tuple[str, str]:
        spec = self.profile.tests[code]
        unit = spec["vendor_unit"]
        if specimen.startswith("QC-"):
            _, test, level = specimen.split("-")
            target = self.profile.data["qc"][test][level]
            shift = float(self.faults.get("qc_shift", {}).get(test, 0.0))
            self._qc_runs += 1
            scatter = (zlib.crc32(f"{specimen}{self._qc_runs}".encode()) % 801 / 1000) - 0.4
            return f"{target['mean'] + (shift + scatter) * target['sd']:.3f}", unit
        drift = self.faults.get("unit_drift", {})
        if code in drift:
            return f"{drift[code]['value']}", drift[code]["unit"]
        value = CRITICAL_VALUES[code] if first and specimen in self.faults.get("critical", []) else NORMAL_VALUES[code]
        return f"{value:g}", unit

    def _flags(self, specimen: str) -> list[str]:
        to_vendor = {v: k for k, v in self.profile.data.get("condition_flags", {}).items()}
        return ["N"] + [to_vendor[c] for c in ("foam", "clot", "short_sample")
                        if specimen in self.faults.get(c, []) and c in to_vendor]

    def result_message(self, specimen: str, tests: list[str], control_id: str) -> str:
        if self.profile.protocol == "astm":
            recs: list[list[str]] = [
                ["H", "", "", f"{self.profile.data['model']}^{self.profile.device_id}", "", "", "", "", "", "",
                 "P", "1", TS],
                ["P", "1", self.patient_for(specimen)],
                ["O", "1", specimen, "", "^^^" + "\\^^^".join(tests), "R"]]
            for i, code in enumerate(tests, start=1):
                value, unit = self._value(specimen, code, first=(i == 1))
                recs.append(["R", str(i), f"^^^{code}", value, unit, "", "\\".join(self._flags(specimen)), "", "F",
                             "", "", "", TS, self.profile.device_id])
            recs.append(["L", "1", "N"])
            return astm.build_message(recs)
        segs = [["MSH", "^~\\&", self.profile.data["model"], "LAB", "HOST", "LAB", TS, "", "OUL^R22^OUL_R22",
                 control_id, "P", "2.5.1"],
                ["PID", "1", "", f"{self.patient_for(specimen)}^^^HOSP^MR"],
                ["SPM", "1", f"{specimen}^LAB", "", "BLD^Whole blood^HL70487"],
                ["OBR", "1", "", specimen, "CBC^Complete blood count^L"]]
        for i, code in enumerate(tests, start=1):
            value, unit = self._value(specimen, code, first=(i == 1))
            segs.append(["OBX", str(i), "NM", f"{code}^{code}^L", "", value, f"{unit}^{unit}^UCUM", "",
                         "~".join(self._flags(specimen)), "", "", "F", "", "", TS])
        return "\r".join("|".join(s) for s in segs) + "\r"


MONITOR_NORMAL = {"MDC_ECG_HEART_RATE": "78", "MDC_PULS_OXIM_SAT_O2": "97", "MDC_PRESS_BLD_NONINV_SYS": "122",
                  "MDC_PRESS_BLD_NONINV_DIA": "76", "MDC_RESP_RATE": "16", "MDC_TEMP": "36.8"}
MONITOR_ARTEFACT = {"MDC_PULS_OXIM_SAT_O2": "62", "MDC_ECG_HEART_RATE": "212"}
MONITOR_TS = "20261019103000"


class SimBedsideMonitor:
    def __init__(self, profile: Profile, faults: dict | None = None):
        self.profile = profile
        self.faults = faults or {}
        self._ctl = itertools.count(1)
        self._set = itertools.count(1)

    @property
    def online(self) -> bool:
        return not self.faults.get("offline", False)

    def association(self) -> dict:
        assoc = dict(self.profile.data["association"])
        if self.faults.get("unassociated"):
            assoc["patient_id"] = ""
        return assoc

    def push(self) -> tuple[str, bytes]:
        """One periodic ORU^R01, MLLP-framed, as the monitor would send it."""
        set_id = f"VS-{self.profile.device_id}-{next(self._set):03d}"
        assoc = self.association()
        pid = f"{assoc['patient_id']}^^^HOSP^MR" if assoc["patient_id"] else ""
        segs = [["MSH", "^~\\&", self.profile.data["model"], "ICU", "HOST", "HOSP", MONITOR_TS, "",
                 "ORU^R01^ORU_R01", f"MON{next(self._ctl):05d}", "P", "2.5.1"],
                ["PID", "1", "", pid], ["PV1", "1", "I", assoc["location"]],
                ["OBR", "1", "", set_id, "VITALS^Vital signs^L"]]
        art, lo = self.faults.get("artefact", []), self.faults.get("lead_off", [])
        for i, (code, value) in enumerate(MONITOR_NORMAL.items(), start=1):
            flags = ["N"]
            if code in art:
                value, flags = MONITOR_ARTEFACT.get(code, value), ["ART"]
            elif code in lo:
                flags = ["LO"]
            unit = self.profile.tests[code]["vendor_unit"]
            segs.append(["OBX", str(i), "NM", f"{code}^{code}^MDC", "", value, f"{unit}^{unit}^MDC", "",
                         "~".join(flags), "", "", "F", "", "", MONITOR_TS])
        return set_id, hl7.mllp_wrap("\r".join("|".join(s) for s in segs) + "\r")
