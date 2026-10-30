"""Simulated clinical instruments that emit real wire formats.

* ``SimAnalyzer``: an analyzer that produces ASTM E1394 or HL7 OUL^R22 result
  messages for a specimen or QC run, and accepts host orders into a worklist.
* ``SimBedsideMonitor``: a monitor that pushes periodic HL7 ORU^R01 vital-sign
  messages with IEEE 11073 (MDC) terms.

Faults are injected into the instrument's behaviour or onto the wire, never by
short-circuiting a decoder:

    analyzer: offline, line_noise_naks, nak_storm, foam/clot/short_sample (specimen ids),
              critical (specimen ids), unit_drift {code: {value, unit}}, qc_shift {test: z},
              patient_mismatch (specimen ids),
              probe_clog ("soft": a probe wash clears it, "hard": only hands on the probe do),
              new_lot {test: lot} (a reagent lot loaded whose calibration has not been run),
              incubator_drift {after: specimen, value: degrees C} (the incubator drifts once that specimen has run),
              hw_fault (a vendor code such as "E-612": the analyzer stops, and a reset does not clear it),
              start_of_day (the daily and weekly maintenance is due), reagent_empty (tests whose pack is empty)

The analyzer also has an operating state, the part its vendor's remote service
works with: sensors, an event log with vendor error codes, reagents on board,
calibrations, and remote and hands-on maintenance. Physical causes live here;
the driver sees only what the instrument reports.
    monitor:  offline, artefact (MDC codes), lead_off (MDC codes), unassociated
"""

from __future__ import annotations

import itertools
import zlib

from ..drivers.dialects import Profile
from ..protocols import astm, hl7, lis_orders

DEFAULT_PATIENTS = {"S-1001": "P-001", "S-1002": "P-002", "H-2001": "P-003",
                    "S-1101": "P-011", "S-1102": "P-012", "S-1103": "P-013"}
CLOG_PRESSURE_KPA = -62.0
CLOG_QC_FACTOR = 0.6          # a blocked probe aspirates too little
LOT_BIAS = 1.12               # a new lot read against the old lot's calibration
TEMP_BIAS = 1.10              # reaction rates at the wrong incubator temperature
NORMAL_VALUES = {"GLU": 5.2, "K": 4.2, "WBC": 7.1, "HGB": 140.0, "PLT": 250.0}
CRITICAL_VALUES = {"GLU": 33.0, "K": 6.9, "WBC": 0.9, "HGB": 55.0, "PLT": 12.0}
TS = "20261019101500"


class SimAnalyzer:
    def __init__(self, profile: Profile, faults: dict | None = None):
        self.profile = profile
        self.faults = faults or {}
        self.worklist: dict[str, dict] = {}
        self._qc_runs = 0
        ops = profile.data.get("operations", {})
        self.sensor_specs = {k: dict(v) for k, v in ops.get("sensors", {}).items()}
        self.reagents = {k: dict(v) for k, v in ops.get("reagents", {}).items()}
        self.calibration = {k: dict(v) for k, v in ops.get("calibration", {}).items()}
        for test, lot in self.faults.get("new_lot", {}).items():
            self.reagents[test]["lot"] = lot
        for test in self.faults.get("reagent_empty", []):
            self.reagents[test]["tests_left"] = 0
        self.clog = self.faults.get("probe_clog")       # None, "soft" or "hard"
        self.drift = dict(self.faults.get("incubator_drift", {}))
        self.drifting = False
        self.hw_fault = self.faults.get("hw_fault", "")
        self.resets = 0
        self.due = set(ops.get("schedule", {})) if self.faults.get("start_of_day") else set()
        self.events: list[dict] = []
        self.run_log: dict[str, dict] = {}              # message id -> {specimen, physical faults active during it}
        self.patient_runs: list[dict] = []              # every patient run started, with the maintenance due then
        self.hands_on: list[dict] = []                  # work a person did at the instrument
        self._event_seq = itertools.count(1)
        if self.hw_fault:
            self._event(self.hw_fault, "analyzer stopped on a hardware fault")

    @property
    def online(self) -> bool:
        return not self.faults.get("offline", False)

    # --- operating state ---------------------------------------------------

    def _event(self, code: str, text: str, **extra) -> None:
        self.events.append({"seq": next(self._event_seq), "code": code, "text": text, **extra})

    def sensors(self) -> dict:
        out = {}
        for name, spec in self.sensor_specs.items():
            value = spec["value"]
            if name == "probe_pressure_kpa" and self.clog:
                value = CLOG_PRESSURE_KPA
            if name == "incubator_c" and self.drifting:
                value = self.drift["value"]
            out[name] = {"value": value, "normal": spec["normal"]}
        return out

    def mode(self) -> dict:
        return {"mode": "error", "code": self.hw_fault} if self.hw_fault else {"mode": "ready", "code": ""}

    def schedule(self) -> dict:
        return {task: {"interval": every, "due": task in self.due}
                for task, every in self.profile.data.get("operations", {}).get("schedule", {}).items()}

    def lot_mismatch(self, test: str) -> bool:
        return test in self.calibration and self.calibration[test]["lot"] != self.reagents.get(test, {}).get("lot")

    def active_faults(self, tests: list[str]) -> list[str]:
        return ((["probe_clog"] if self.clog else []) + (["incubator_drift"] if self.drifting else [])
                + [f"uncalibrated_lot:{t}" for t in tests if self.lot_mismatch(t)])

    def begin_run(self, specimen: str, tests: list[str]) -> dict | None:
        """The analyzer starts a run, or refuses to: stopped on a hardware fault, or a reagent pack empty."""
        if not specimen.startswith("QC-"):
            self.patient_runs.append({"specimen": specimen, "due": sorted(self.due)})
        if self.hw_fault:
            self._event(self.hw_fault, "run refused: analyzer stopped on a hardware fault", specimen=specimen)
            return {"status": "error", "error": self.hw_fault, "detail": "the analyzer is stopped; see the event log"}
        empty = [t for t in tests if self.reagents.get(t, {}).get("tests_left", 1) == 0]
        if empty:
            self._event("R-120", f"reagent pack empty for {empty}", specimen=specimen)
            return {"status": "error", "error": "R-120", "detail": f"reagent pack empty for {empty}"}
        return None

    def reset(self) -> dict:
        self.resets += 1
        self._event("R-001", "analyzer reset")
        if self.hw_fault:
            self._event(self.hw_fault, "hardware fault present again after reset")
            return {"status": "error", "error": self.hw_fault, "detail": "the fault came back after the reset"}
        return {"status": "ok", "mode": "ready"}

    def maintenance(self, task: str) -> dict:
        if task == "probe_wash":
            if self.clog == "soft":
                self.clog = None
            self._event("M-101", "probe wash completed")
            if self.clog:
                self._event("E-217", "aspiration pressure still out of range after probe wash")
        else:
            self._event("M-102", f"{task} completed")
        self.due.discard(task)
        return {"status": "ok", "task": task}

    def calibrate(self, test: str) -> dict:
        if self.clog:
            self._event("E-405", f"calibration failed for {test}: aspiration", test=test)
            return {"status": "error", "error": "E-405", "detail": f"calibration of {test} failed: aspiration"}
        self.calibration[test] = {**self.calibration.get(test, {}), "lot": self.reagents[test]["lot"],
                                  "valid_until": "2026-12-01"}
        self._event("C-100", f"calibration accepted for {test} lot {self.reagents[test]['lot']}", test=test)
        return {"status": "ok", "test": test, "lot": self.reagents[test]["lot"]}

    def perform_by_hand(self, task: str, by: str) -> dict:
        """A person at the instrument. Only the human side of the operator channel gets here."""
        if task == "probe_clean_manual":
            self.clog = None
        if task == "load_reagent_pack":
            spare = self.profile.data.get("operations", {}).get("spare_lots", {})
            for test, pack in self.reagents.items():
                if pack["tests_left"] == 0:
                    self.reagents[test] = {**pack, "lot": spare[test], "tests_left": 500}
        self.hands_on.append({"task": task, "by": by})
        self._event("M-201", f"manual maintenance recorded: {task}", by=by)
        return {"status": "ok", "task": task, "by": by}

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
            value = (target["mean"] + (shift + scatter) * target["sd"]) * self._physical_factor(code)
            return f"{value:.3f}", unit
        drift = self.faults.get("unit_drift", {})
        if code in drift:
            return f"{drift[code]['value']}", drift[code]["unit"]
        value = CRITICAL_VALUES[code] if first and specimen in self.faults.get("critical", []) else NORMAL_VALUES[code]
        return f"{value * self._physical_factor(code):g}", unit

    def _physical_factor(self, code: str) -> float:
        return ((CLOG_QC_FACTOR if self.clog else 1.0) * (LOT_BIAS if self.lot_mismatch(code) else 1.0)
                * (TEMP_BIAS if self.drifting else 1.0))

    def _flags(self, specimen: str) -> list[str]:
        to_vendor = {v: k for k, v in self.profile.data.get("condition_flags", {}).items()}
        flags = ["N"] + [to_vendor[c] for c in ("foam", "clot", "short_sample")
                         if specimen in self.faults.get(c, []) and c in to_vendor]
        if self.clog and "short_sample" in to_vendor and to_vendor["short_sample"] not in flags:
            flags.append(to_vendor["short_sample"])         # the probe draws too little: looks like a short sample
        return flags

    def result_message(self, specimen: str, tests: list[str], control_id: str) -> str:
        self.run_log[control_id] = {"specimen": specimen, "faults": self.active_faults(tests)}
        if self.clog:
            self._event("E-217", "aspiration pressure out of range at the sample probe", specimen=specimen,
                        message_id=control_id)
        if self.drifting:
            self._event("E-310", f"incubator at {self.drift['value']} C during the run", specimen=specimen,
                        message_id=control_id)
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
            self._after_run(specimen)
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
        self._after_run(specimen)
        return "\r".join("|".join(s) for s in segs) + "\r"

    def _after_run(self, specimen: str) -> None:
        if self.drift and specimen == self.drift["after"]:
            self.drifting = True              # the heater controller fails silently; the next run shows it


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
