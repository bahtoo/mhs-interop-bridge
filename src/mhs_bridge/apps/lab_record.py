"""Lab record: accept research results only when the data is valid.

An application on top of MHS. It reads the liquid handler's plate map and the
plate reader's export through MHS, like any other client, and combines them:
expected concentrations from one device, measured absorbance from another, foam
flags from the first, saturation from the second. No single device can make
that judgement, and it is not a hardware concern, so it is not in MHS or in a
driver.
"""

from __future__ import annotations

from typing import Any

from ..mhs_shim import Decision, Manifest, Param, Procedure, State
from .base import validate


class LabRecordApp:
    app_id = "lab_record"

    def __init__(self, runtime: Any, operator: Any, liquid_handler: str = "lh-01", reader: str = "reader-01"):
        self.runtime, self.operator = runtime, operator
        self.lh, self.reader = liquid_handler, reader
        self.accepted: list[dict] = []
        operator.attach(self)

    def manifest(self) -> Manifest:
        return Manifest(self.app_id, "application", "mhs-interop-bridge", "lab record", "MHS client", [
            State("series_fit:<plate>:<row>", "object", "linear fit of absorbance against expected concentration for a "
                                                        "dilution row, with saturated and foam-invalid wells"),
            State("accepted", "list", "series accepted so far")], [
            Procedure("accept_series", "Record a dilution row as a valid result.",
                      [Param("plate", "string", "plate id"), Param("row", "string", "row letter")],
                      safety_checks=["no saturated wells", "no wells that foamed", "R2 at least 0.98"])])

    def read(self, state: str) -> Any:
        if state.startswith("series_fit:"):
            _, plate, row = state.split(":")
            return self.series_fit(plate, row)
        if state == "accepted":
            return self.accepted
        raise KeyError(state)

    def series_fit(self, plate: str, row: str) -> dict:
        plate_map = self.runtime.read(self.lh, "PlateMap", actor=f"app:{self.app_id}")
        readings = self.runtime.read(self.reader, f"last_read:{plate}", actor=f"app:{self.app_id}")
        status = self.runtime.read(self.reader, "instrument_status", actor=f"app:{self.app_id}")
        if not isinstance(readings, dict) or not readings or "error" in readings:
            return {"error": f"no readings for plate {plate}"}
        sat = float(status.get("saturation", 3.0)) if isinstance(status, dict) else 3.0
        wells = plate_map.get(plate, {}) if isinstance(plate_map, dict) else {}
        pts, saturated, invalid = [], [], []
        for c in range(1, 13):
            wid = f"{row}{c}"
            w = wells.get(wid)
            if not w or wid not in readings:
                continue
            if w["foam"]:
                invalid.append(wid)
            elif readings[wid] >= sat - 1e-9:
                saturated.append(wid)
            else:
                pts.append((w["conc_ug_ml"], readings[wid]))
        top = wells.get(f"{row}1", {}).get("conc_ug_ml", 0.0)
        base = {"error": None, "points": len(pts), "saturated_wells": saturated, "invalid_wells": invalid,
                "top_conc_ug_ml": top}
        if len(pts) < 3:
            return {**base, "slope": 0.0, "intercept": 0.0, "r2": 0.0}
        n = len(pts)
        mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
        sxx = sum((x - mx) ** 2 for x, _ in pts)
        slope = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx else 0.0
        intercept = my - slope * mx
        ss_tot = sum((y - my) ** 2 for _, y in pts)
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in pts)
        return {**base, "slope": round(slope, 5), "intercept": round(intercept, 4),
                "r2": round(1 - ss_res / ss_tot, 5) if ss_tot else 0.0}

    def check(self, procedure: str, args: dict) -> Decision:
        if procedure != "accept_series":
            return Decision.block("unknown_procedure", procedure)
        fit = self.series_fit(args["plate"], args["row"])
        if fit.get("error"):
            return Decision.block("no_data", fit["error"])
        if fit["invalid_wells"]:
            return Decision.block("data_integrity", f"wells {fit['invalid_wells']} foamed; their readings are invalid",
                                  "Run a fresh row with gentler liquid handling.")
        if fit["saturated_wells"]:
            return Decision.block("data_integrity", f"wells {fit['saturated_wells']} are saturated",
                                  "Compress the concentration range and run a fresh row.")
        if fit["r2"] < 0.98:
            return Decision.block("data_integrity", f"fit R2 {fit['r2']} is below 0.98")
        return Decision.ok()

    def invoke(self, procedure: str, args: dict, actor: str = "agent") -> dict:
        d = validate(self.manifest(), procedure, args) or self.check(procedure, args)
        if not d.allowed:
            self.runtime.record(actor, "app", self.app_id, procedure, args, "blocked", d.rule, d.reason)
            return {"status": "blocked", "rule": d.rule, "reason": d.reason, "hint": d.hint}
        fit = self.series_fit(args["plate"], args["row"])
        if fit.get("error"):
            return {"status": "error", "error": fit["error"]}
        self.accepted.append({"plate": args["plate"], "row": args["row"], **fit})
        self.runtime.record(actor, "app", self.app_id, procedure, args, "executed")
        return {"status": "ok", "accepted": True, "r2": fit.get("r2")}

    def execute_approved(self, procedure: str, args: dict, approver: str) -> dict:   # pragma: no cover
        return {"status": "error", "error": "nothing in this app needs approval"}
