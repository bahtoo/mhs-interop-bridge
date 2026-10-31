"""MHS drivers for a CT scanner's service surface and for the contrast injector beside it.

The CT driver exposes what the vendor's remote service and the radiographer's QA
screen see: sensors, the event log, what is in the gantry, daily QA results and
the QA image. Its procedures are the remote ones: tube warm-up, air calibration,
a QA scan of the water phantom, and release for clinical use. There is no
procedure to scan a patient. Placing and removing the phantom are hands-on tasks.

Interlocks: no exposure on a cold tube; no air calibration with anything in the
gantry; no QA scan without the phantom; no release for clinical use without a QA
pass since the last air calibration, or with the phantom still on the table.

The injector is ``patient_connected``: states only, and the runtime refuses any invoke.
"""

from __future__ import annotations

from typing import Any

from ..mhs_shim import Decision, Manifest, Procedure, State
from ..sim.ct import SimCT, SimInjector, image_state
from .dialects import Profile


class CTDriver:
    def __init__(self, profile: Profile, instrument: SimCT):
        self.profile = profile
        self.device_id = profile.device_id
        self.instrument = instrument

    @property
    def online(self) -> bool:
        return self.instrument.online

    @property
    def ops(self) -> dict:
        return self.profile.data.get("operations", {})

    def manifest(self) -> Manifest:
        return Manifest(
            self.device_id, self.profile.device_type, self.profile.data["vendor"], self.profile.data["model"],
            "vendor remote service and QA interface (images to PACS over DICOM, outside this surface)",
            [State("status", "string", "online or offline"),
             State("sensors", "object", "sensor readings with their normal ranges"),
             State("event_log", "list", "the scanner's event log: vendor codes, newest last"),
             State("error_codes", "object", "what each vendor event code means"),
             State("gantry", "object", "what is on the table and in the gantry"),
             State("qa_status", "object", "the latest daily QA scan, the limits, and whether QA passes now"),
             State("qa_image", "image", "the latest QA slice through the water phantom (PNG)"),
             State("clinical_use", "object", "whether the scanner is released for patients"),
             State("maintenance_tasks", "object", "remote procedures, and hands-on tasks (a person at the scanner "
                                                  "does them; request a work instruction)"),
             State("field_service", "object", "faults that need a field engineer, and the part")],
            [Procedure("tube_warmup", "Warm the X-ray tube up before the first exposure of the day.", []),
             Procedure("air_calibration", "Calibrate every detector channel with nothing in the beam.", [],
                       safety_checks=["the gantry and table are empty", "the tube is warm"]),
             Procedure("scan_qa_phantom", "Scan the water phantom and measure mean CT number, noise and uniformity.",
                       [], safety_checks=["the water phantom is on the table", "the tube is warm"]),
             Procedure("release_for_clinical_use", "Release the scanner for patient scanning.", [],
                       safety_checks=["daily QA passes since the last air calibration", "the table is clear"])],
            self.profile.tags, self.profile.data.get("risk_class", ""), driver="ct")

    def read(self, state: str) -> Any:
        ct = self.instrument
        if state == "status":
            return "online" if self.online else "offline"
        if state == "sensors":
            return ct.sensors()
        if state == "event_log":
            return ct.events[-20:]
        if state == "error_codes":
            return self.ops.get("error_codes", {})
        if state == "gantry":
            return {"on_table": ct.table}
        if state == "qa_status":
            return ct.qa_status()
        if state == "qa_image":
            return image_state(ct)
        if state == "clinical_use":
            return ct.clinical_use()
        if state == "maintenance_tasks":
            return {"remote": self.ops.get("remote_tasks", {}), "hands_on": self.ops.get("hands_on_tasks", {})}
        if state == "field_service":
            return self.ops.get("field_service", {})
        raise KeyError(state)

    def precheck(self, procedure: str, args: dict):
        ct = self.instrument
        if procedure in ("air_calibration", "scan_qa_phantom") and not ct.tube_warm:
            return Decision.block("tube_cold", "the X-ray tube is cold", "Run tube_warmup before the first exposure.")
        if procedure == "air_calibration" and ct.table is not None:
            return Decision.block("gantry_not_clear", f"{ct.table} is in the gantry",
                                  "An air calibration needs nothing in the beam: whatever is there is calibrated in "
                                  "as air. Ask for it to be removed first.")
        if procedure == "scan_qa_phantom" and ct.table != "water_phantom":
            return Decision.block("no_phantom", "the water phantom is not on the table",
                                  "Ask the radiographer to place it (a work instruction).")
        if procedure == "release_for_clinical_use":
            if not ct.qa_status()["passing"]:
                return Decision.block("qa_not_passed", "daily QA does not pass since the last air calibration",
                                      "Fix the cause, repeat QA, and release only on a pass. If it cannot pass, keep "
                                      "the scanner out of clinical use and open a service case.")
            if ct.table is not None:
                return Decision.block("gantry_not_clear", f"{ct.table} is still on the table",
                                      "Ask for the phantom to be removed before patients.")
        return None

    def write(self, procedure: str, args: dict) -> dict:
        ct = self.instrument
        if procedure == "tube_warmup":
            return ct.warmup()
        if procedure == "air_calibration":
            return ct.air_calibration()
        if procedure == "scan_qa_phantom":
            return ct.scan_qa()
        if procedure == "release_for_clinical_use":
            return ct.release()
        raise KeyError(procedure)

    def perform_by_hand(self, task: str, by: str) -> dict:
        """Not a procedure: the radiographer did ``task`` at the scanner."""
        if task not in self.ops.get("hands_on_tasks", {}):
            return {"status": "error", "error": "UNKNOWN_TASK", "detail": task}
        return self.instrument.perform_by_hand(task, by)


class InjectorDriver:
    def __init__(self, profile: Profile, instrument: SimInjector):
        self.profile = profile
        self.device_id = profile.device_id
        self.instrument = instrument

    @property
    def online(self) -> bool:
        return self.instrument.online

    def manifest(self) -> Manifest:
        return Manifest(self.device_id, self.profile.device_type, self.profile.data["vendor"],
                        self.profile.data["model"], "vendor status interface (read only)",
                        [State("status", "string", "online or offline"),
                         State("injector", "object", "armed, syringe volume, pressure limit, last injection")],
                        [], self.profile.tags, self.profile.data.get("risk_class", ""), driver="injector")

    def read(self, state: str) -> Any:
        if state == "status":
            return "online" if self.online else "offline"
        if state == "injector":
            return self.instrument.state()
        raise KeyError(state)

    def write(self, procedure: str, args: dict) -> dict:     # pragma: no cover - the runtime refuses first
        raise PermissionError("patient-connected devices accept no procedures")
