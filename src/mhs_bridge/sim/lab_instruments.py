"""Simulated research-lab instruments, each speaking its own interface.

* ``SimLiquidHandler``: a SiLA 2 server implementing LiquidHandlingService.
* ``SimPlateMover``: a SiLA 2 server implementing PlateHandlingService.
* ``SimFileDropReader``: an absorbance reader with no API at all. Its control
  software watches an inbox folder for command files and writes CSV exports,
  a top-view photo of the plate and a status file, the way many legacy
  instruments are automated in practice.

With liquid level detection off (``lld = False``, fault ``lld_off``) the liquid
handler still foams viscous samples but reports nothing: no ``FoamDetected``, and
no foam in its plate map. The bubbles are still in the world, and in the photo.

The SiLA servers expose ``call_command`` and ``get_property`` with the same
shape as a SiLA 2 client stub, so the driver code that talks to them is the
code that would talk to a real server over gRPC.
"""

from __future__ import annotations

import csv
import io
import json
import zlib
from pathlib import Path

from ..protocols.sila2 import SiLA2ExecutionError
from .imaging import render_plate
from .world import FOAM_FLOW_UL_S, MAX_GENTLE_MIX, LabWorld


class SimLiquidHandler:
    """SiLA 2 server: LiquidHandlingService v1."""

    FEATURE = "LiquidHandlingService"

    def __init__(self, world: LabWorld, device_id: str = "lh-01"):
        self.world = world
        self.device_id = device_id
        self.lld = True             # liquid level detection: how this instrument knows about foam at all
        world.online.setdefault(device_id, True)

    def get_property(self, feature: str, prop: str) -> str:
        if prop == "PlateMap":
            return json.dumps({pid: {wid: {"volume_ul": round(w.volume_ul, 2), "conc_ug_ml": round(w.conc_ug_ml, 3),
                                           "liquid": w.liquid, "foam": w.foam and self.lld}
                                     for wid, w in p.wells.items() if w.volume_ul > 0}
                               for pid, p in self.world.plates.items()})   # contents it dispensed, wherever the plate is now
        if prop == "DeckLayout":
            return json.dumps({"labware": [pid for pid, p in self.world.plates.items() if p.location == self.device_id],
                               "reservoir": {k: {"volume_ul": w.volume_ul, "conc_ug_ml": w.conc_ug_ml, "liquid": w.liquid}
                                             for k, w in self.world.plates["RES"].wells.items()}})
        raise KeyError(prop)

    def call_command(self, feature: str, command: str, params: dict) -> dict:
        if command != "Transfer":
            raise KeyError(command)
        src_loc, dst_loc = params["Source"], params["Target"]
        for loc in (src_loc, dst_loc):
            if not self.world.has(loc) or self.world.plates[loc.split(":")[0]].location != self.device_id:
                raise SiLA2ExecutionError("PositionNotFound", f"{loc} is not on the deck")
        src, dst = self.world.well(src_loc), self.world.well(dst_loc)
        vol, flow, mix = float(params["Volume"]), float(params["FlowRate"]), int(params.get("MixCycles", 0))
        if src.foam and self.lld:
            raise SiLA2ExecutionError("FoamDetected", f"liquid level detection hit foam in {src_loc}", {"Well": src_loc})
        if src.volume_ul < vol:
            raise SiLA2ExecutionError("InsufficientVolume", f"{src_loc} holds {src.volume_ul} uL")
        idx = len(self.world.transfers)
        self.world.transfers.append({"device": self.device_id, "src": src_loc, "dst": dst_loc,
                                     "vol": vol, "flow": flow, "mix": mix})
        total = dst.volume_ul + vol
        dst.conc_ug_ml = (dst.conc_ug_ml * dst.volume_ul + src.conc_ug_ml * vol) / total
        if src.liquid not in ("aqueous", "empty"):
            dst.liquid = src.liquid
        elif dst.liquid == "empty":
            dst.liquid = "aqueous"
        dst.volume_ul = total       # the hardware does not stop an overfill; that is the driver's job
        src.volume_ul -= vol
        if dst.viscous and (flow > FOAM_FLOW_UL_S or mix > MAX_GENTLE_MIX):
            dst.foam = True
            self.world.foam_events.append((idx, dst_loc))
            if not self.lld:
                self.world.silent_foam.add(dst_loc)
                return {"TargetVolume": round(dst.volume_ul, 2)}      # foamed, and nothing noticed
            raise SiLA2ExecutionError("FoamDetected",
                                      f"bubbles formed in {dst_loc} (viscous liquid, {flow} uL/s, {mix} mix cycles)",
                                      {"Well": dst_loc})
        return {"TargetVolume": round(dst.volume_ul, 2)}


class SimPlateMover:
    """SiLA 2 server: PlateHandlingService v1."""

    FEATURE = "PlateHandlingService"

    def __init__(self, world: LabWorld, device_id: str = "arm-01"):
        self.world = world
        self.device_id = device_id
        world.online.setdefault(device_id, True)

    def get_property(self, feature: str, prop: str) -> str:
        if prop == "PlateLocations":
            return json.dumps({pid: p.location for pid, p in self.world.plates.items() if pid != "RES"})
        raise KeyError(prop)

    def call_command(self, feature: str, command: str, params: dict) -> dict:
        if command != "MovePlate":
            raise KeyError(command)
        plate, dest = params["Plate"], params["Destination"]
        if plate not in self.world.plates or plate == "RES":
            raise SiLA2ExecutionError("PlateNotFound", f"no plate {plate}")
        if not self.world.online.get(dest, False):
            raise SiLA2ExecutionError("DestinationUnavailable", f"{dest} is not ready to receive a plate")
        self.world.plates[plate].location = dest
        self.world.moves.append({"plate": plate, "to": dest})
        return {"Location": dest}


class SimFileDropReader:
    """Absorbance reader automated by file drop. Writes status.json and CSV exports."""

    SLOPE = 0.02           # absorbance per ug/mL (simulated)
    BASELINE = 0.05
    NOISE = 0.03           # +/-3% gain noise plus +/-0.015 AU offset noise, deterministic
    SATURATION = 3.0

    def __init__(self, world: LabWorld, folder: Path, device_id: str = "reader-01"):
        self.world = world
        self.device_id = device_id
        self.folder = Path(folder)
        for sub in ("inbox", "export"):
            (self.folder / sub).mkdir(parents=True, exist_ok=True)
        world.online.setdefault(device_id, True)
        self._runs = 0
        self._write_status()

    def _present(self) -> list[str]:
        return [pid for pid, p in self.world.plates.items() if p.location == self.device_id]

    def _write_status(self, last_error: str = "") -> None:
        (self.folder / "status.json").write_text(json.dumps(
            {"state": "idle", "plates_present": self._present(), "saturation": self.SATURATION,
             "last_error": last_error}), encoding="utf-8")

    def process_inbox(self) -> None:
        """One tick of the vendor software: refresh status, run any command files."""
        if not self.world.online.get(self.device_id, True):
            return
        self._write_status()
        for cmd_file in sorted((self.folder / "inbox").glob("*.json")):
            cmd = json.loads(cmd_file.read_text(encoding="utf-8"))
            cmd_file.unlink()
            plate_id, wl = cmd["plate"], int(cmd["wavelength_nm"])
            if plate_id not in self._present():
                self._write_status(f"no plate {plate_id} in reader")
                continue
            self._runs += 1
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Well", f"Abs{wl}"])
            for wid, well in self.world.plates[plate_id].wells.items():
                a = self.BASELINE + self.SLOPE * well.conc_ug_ml if well.volume_ul else 0.0
                j1 = zlib.crc32(f"{plate_id}{wid}{self._runs}g".encode()) % 2001 / 1000 - 1.0
                j2 = zlib.crc32(f"{plate_id}{wid}{self._runs}o".encode()) % 2001 / 1000 - 1.0
                if well.volume_ul:
                    a = a * (1 + self.NOISE * j1) + 0.015 * j2       # gain and offset noise
                if well.foam:
                    a += 0.35          # bubbles scatter light
                w.writerow([wid, f"{min(self.SATURATION, a):.3f}"])
            (self.folder / "export" / f"{plate_id}_{self._runs:04d}.csv").write_text(buf.getvalue(), encoding="utf-8",
                                                                                    newline="")
            (self.folder / "export" / f"{plate_id}_{self._runs:04d}.png").write_bytes(
                render_plate(self.world.plates[plate_id]))
            self.world.reads.append({"plate": plate_id, "wavelength": wl, "after": len(self.world.transfers)})
            self.world.readings[plate_id] = {
                row["Well"]: float(row[f"Abs{wl}"]) for row in csv.DictReader(io.StringIO(buf.getvalue()))}
            self._write_status()
