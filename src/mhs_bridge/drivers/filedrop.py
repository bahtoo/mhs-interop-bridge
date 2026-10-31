"""Driver for instruments that have no API: automation by file drop.

Many instruments in real labs can only be automated through their vendor
software's folders: write a command file into a watched inbox, wait for an
export, parse it, and read a status file. The MHS research preview describes
the same situation (a plate reader with no programmatic interface). This driver
is the reusable pattern for it, shown with an absorbance plate reader.
"""

from __future__ import annotations

import base64
import csv
import io
import itertools
import json
from pathlib import Path
from typing import Callable

from ..mhs_shim import Decision, Manifest, Param, Procedure, State


class FileDropReaderDriver:
    def __init__(self, profile: dict, folder: str | Path, process: Callable[[], None] = lambda: None,
                 is_online: Callable[[], bool] = lambda: True):
        self.profile = profile
        self.device_id = profile["device_id"]
        self.folder = Path(folder)
        self._process = process          # in a lab: the vendor software runs on its own; here we tick it
        self._is_online = is_online
        self._ids = itertools.count(1)

    @property
    def online(self) -> bool:
        return self._is_online()

    def _status(self) -> dict:
        self._process()      # let the vendor software refresh its status file
        return json.loads((self.folder / "status.json").read_text(encoding="utf-8"))

    def manifest(self) -> Manifest:
        lim = self.profile["limits"]["wavelength_nm"]
        return Manifest(
            self.device_id, self.profile["device_type"], self.profile["vendor"], self.profile["model"],
            "file drop (command JSON in, CSV export and status JSON out)",
            [State("status", "string", "online or offline"),
             State("instrument_status", "object", "the instrument's own status file: plates present, saturation, last error"),
             State("last_read:<plate>", "object", "absorbance per well from the latest export for that plate"),
             State("last_image:<plate>", "image", "top-view photo of the plate taken at its latest read (PNG)")],
            [Procedure("ReadPlate", "Read absorbance for every well of a plate that is in the reader.",
                       [Param("Plate", "string", "plate id"),
                        Param("WavelengthNm", "number", "wavelength", lim["min"], lim["max"], "nm")],
                       errors={"NoExport": "the instrument produced no export; see instrument_status.last_error"},
                       safety_checks=["plate must be present in the reader according to the instrument status file"])],
            self.profile.get("tags", []), self.profile.get("risk_class", ""), driver="filedrop")

    def read(self, state: str):
        if state == "status":
            return "online" if self.online else "offline"
        if state == "instrument_status":
            return self._status()
        if state.startswith("last_read:"):
            plate = state.split(":", 1)[1]
            exports = sorted((self.folder / "export").glob(f"{plate}_*.csv"))
            if not exports:
                return {}
            rows = csv.reader(io.StringIO(exports[-1].read_text(encoding="utf-8")))
            next(rows)
            return {well: float(v) for well, v in rows}
        if state.startswith("last_image:"):
            plate = state.split(":", 1)[1]
            photos = sorted((self.folder / "export").glob(f"{plate}_*.png"))
            if not photos:
                return {}
            return {"media_type": "image/png", "plate": plate, "taken": photos[-1].stem,
                    "data_base64": base64.b64encode(photos[-1].read_bytes()).decode("ascii")}
        raise KeyError(state)

    def precheck(self, procedure: str, args: dict):
        if procedure == "ReadPlate" and args.get("Plate") not in self._status().get("plates_present", []):
            return Decision.block("plate_location", f"plate {args.get('Plate')} is not in {self.device_id}",
                                  "Move the plate to the reader first.")
        return None

    def write(self, procedure: str, args: dict) -> dict:
        if procedure != "ReadPlate":
            raise KeyError(procedure)
        before = set((self.folder / "export").glob("*.csv"))
        cmd = self.folder / "inbox" / f"cmd_{next(self._ids):04d}.json"
        cmd.write_text(json.dumps({"plate": args["Plate"], "wavelength_nm": args["WavelengthNm"]}), encoding="utf-8")
        self._process()
        new = sorted(set((self.folder / "export").glob("*.csv")) - before)
        if not new:
            return {"status": "error", "error": "NoExport", "detail": self._status().get("last_error", "")}
        return {"status": "ok", "plate": args["Plate"], "export": new[-1].name}
