"""A simulated CT scanner and contrast injector, for radiology device operations.

The CT's daily quality assurance is a scan of a water phantom: the mean CT number
of water should be close to 0 HU, the noise low, and the edge of the phantom should
read the same as its centre (uniformity). The physics that matters here:

* An **air calibration** measures each detector channel with nothing in the beam.
  An out-of-date one shows as cupping or a ring in the phantom image; a new air
  calibration clears it.
* Anything left in the gantry during an air calibration is calibrated in as if it
  were air, and every image after it is wrong. So the phantom has to come out
  first, and only a person can take it out.
* A failing detector module also shows as a ring, but an air calibration does not
  clear it; the calibration logs the channel instead (``D-412``).
* The tube is warmed up before the first exposure of the day.

Faults: ``start_of_day`` (tube cold, daily QA not yet done), ``air_cal_stale``,
``detector_fault``. Physical causes live here; the driver sees only what the
scanner reports: QA numbers, an image, an event log.

``SimInjector`` is the contrast injector beside the scanner. It is patient-connected,
so through MHS it can only be read.
"""

from __future__ import annotations

import base64
import itertools
import math

from ..drivers.dialects import Profile
from .imaging import _png

QA_GOOD = {"mean_hu": 0.4, "noise_sd": 4.8, "uniformity_hu": 1.2}
QA_STALE = {"mean_hu": 1.1, "noise_sd": 4.9, "uniformity_hu": 7.6}          # cupping: centre and edge differ
QA_DETECTOR = {"mean_hu": 0.8, "noise_sd": 5.1, "uniformity_hu": 9.4}       # a ring from one detector module
QA_CONTAMINATED = {"mean_hu": -17.5, "noise_sd": 5.6, "uniformity_hu": 21.0}  # the phantom calibrated in as air
QA_NO_PHANTOM = {"mean_hu": -1000.0, "noise_sd": 2.0, "uniformity_hu": 0.0}  # nothing on the table: air
TUBE_COLD, TUBE_WARM = 0, 22
SIZE, RADIUS = 96, 40


class SimCT:
    def __init__(self, profile: Profile, faults: dict | None = None):
        self.profile = profile
        self.faults = faults or {}
        self.limits = profile.data["qa_limits"]
        start = bool(self.faults.get("start_of_day"))
        self.tube_warm = not start
        self.cal_stale = bool(self.faults.get("air_cal_stale"))
        self.detector_fault = bool(self.faults.get("detector_fault"))
        self.cal_contaminated = False
        self.table: str | None = None                   # what is in the gantry
        self.events: list[dict] = []
        self.qa: list[dict] = []
        self.air_cals: list[dict] = []
        self.cold_exposures = 0
        self.released = not start
        self.releases: list[dict] = []                  # each release, with the physical faults present then
        self.hands_on: list[dict] = []
        self.last_image = b""
        self._seq = itertools.count(1)
        if start:
            self._event("W-101", "tube warm-up required before the first exposure")
            self._event("Q-100", "daily QA due")

    @property
    def online(self) -> bool:
        return not self.faults.get("offline", False)

    def _event(self, code: str, text: str, **extra) -> int:
        seq = next(self._seq)
        self.events.append({"seq": seq, "code": code, "text": text, **extra})
        return seq

    # --- what the scanner reports --------------------------------------------

    def sensors(self) -> dict:
        specs = self.profile.data["operations"]["sensors"]
        values = {"tube_heat_pct": TUBE_WARM if self.tube_warm else TUBE_COLD}
        return {n: {"value": values.get(n, s["value"]), "normal": s["normal"]} for n, s in specs.items()}

    def physical_faults(self) -> list[str]:
        return ((["contaminated_air_calibration"] if self.cal_contaminated else [])
                + (["detector_module"] if self.detector_fault else [])
                + (["stale_air_calibration"] if self.cal_stale and not self.cal_contaminated else []))

    def qa_status(self) -> dict:
        latest = self.qa[-1] if self.qa else None
        last_cal = self.air_cals[-1]["seq"] if self.air_cals else 0
        valid = bool(latest and latest["pass"] and latest["seq"] > last_cal)
        return {"latest": latest, "limits": self.limits, "passing": valid,
                "note": "" if valid or not latest or not latest["pass"] else "an air calibration since the last QA"}

    def clinical_use(self) -> dict:
        return {"released": self.released, "history": self.releases[-3:]}

    # --- remote procedures ---------------------------------------------------

    def warmup(self) -> dict:
        self.tube_warm = True
        self._event("W-100", "tube warm-up completed")
        return {"status": "ok", "tube_heat_pct": TUBE_WARM}

    def _expose(self) -> None:
        if not self.tube_warm:
            self.cold_exposures += 1
            self._event("W-102", "exposure on a cold tube")

    def air_calibration(self) -> dict:
        self._expose()
        seq = self._event("C-201", "air calibration completed")
        self.air_cals.append({"seq": seq, "in_gantry": self.table})
        self.released = False
        if self.table is not None:
            self.cal_contaminated = True              # the scanner cannot tell; the images will
        else:
            self.cal_stale = self.cal_contaminated = False
        if self.detector_fault:
            self._event("D-412", "detector module 3: channel gain out of tolerance after air calibration")
        return {"status": "ok", "calibration": "air", "seq": seq}

    def scan_qa(self) -> dict:
        self._expose()
        if self.table != "water_phantom":
            values = QA_NO_PHANTOM
        elif self.cal_contaminated:
            values = QA_CONTAMINATED
        elif self.detector_fault:
            values = QA_DETECTOR
        elif self.cal_stale:
            values = QA_STALE
        else:
            values = QA_GOOD
        lim = self.limits
        checks = {"mean_hu": lim["mean_hu"][0] <= values["mean_hu"] <= lim["mean_hu"][1],
                  "noise_sd": values["noise_sd"] <= lim["noise_sd"],
                  "uniformity_hu": values["uniformity_hu"] <= lim["uniformity_hu"]}
        ok = all(checks.values())
        seq = self._event("Q-300" if ok else "Q-301", "daily QA passed" if ok else
                          f"daily QA failed: {[k for k, v in checks.items() if not v]}")
        result = {"seq": seq, **values, "checks": checks, "pass": ok}
        self.qa.append(result)
        self.last_image = render_phantom(self)
        return {"status": "ok", "qa": result}

    def release(self) -> dict:
        faults = self.physical_faults() + (["phantom_on_table"] if self.table else [])
        self.released = True
        self.releases.append({"seq": self._event("S-100", "released for clinical use"), "faults": faults})
        return {"status": "ok", "released": True}

    def perform_by_hand(self, task: str, by: str) -> dict:
        if task == "place_water_phantom":
            self.table = "water_phantom"
        if task == "remove_water_phantom":
            self.table = None
        self.hands_on.append({"task": task, "by": by})
        self._event("M-201", f"manual task recorded: {task}", by=by)
        return {"status": "ok", "task": task, "by": by}


def render_phantom(ct: SimCT) -> bytes:
    """The QA slice through the phantom, at a narrow water window: grey disc, black air around it."""
    qa = ct.qa[-1]
    cupping = ct.cal_stale and not ct.detector_fault and not ct.cal_contaminated
    ring = ct.detector_fault or ct.cal_contaminated
    px = []
    for y in range(SIZE):
        line = []
        for x in range(SIZE):
            r = math.hypot(x - SIZE / 2, y - SIZE / 2)
            if r > RADIUS or ct.table != "water_phantom":
                line.append((0, 0, 0))
                continue
            hu = qa["mean_hu"]
            if cupping:
                hu += qa["uniformity_hu"] * (r / RADIUS) ** 2 - qa["uniformity_hu"] / 2
            if ring and abs(r - 18) < 1.6:
                hu += 3 * qa["uniformity_hu"]
            noise = ((x * 7919 + y * 104729) % 97 / 97 - 0.5) * qa["noise_sd"]
            v = max(0, min(255, int(128 + (hu + noise) * 4)))
            line.append((v, v, v))
        px.append(line)
    return _png(px)


def image_state(ct: SimCT) -> dict:
    if not ct.last_image:
        return {"error": "no QA scan yet today"}
    return {"media_type": "image/png", "taken": f"QA scan {ct.qa[-1]['seq']}",
            "data_base64": base64.b64encode(ct.last_image).decode("ascii")}


class SimInjector:
    def __init__(self, profile: Profile, faults: dict | None = None):
        self.profile = profile
        self.faults = faults or {}

    @property
    def online(self) -> bool:
        return not self.faults.get("offline", False)

    def state(self) -> dict:
        return {"armed": False, "syringe_ml": 0, "pressure_limit_psi": 325, "last_injection": "none today"}
