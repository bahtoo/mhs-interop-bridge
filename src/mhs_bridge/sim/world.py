"""Physical ground truth for the simulated lab.

Simulators act on this world; drivers only see what the simulated instruments
report. The eval invariants read the world directly, which is how they check
what physically happened without trusting any driver or rule.

The physical model is small on purpose. It captures the failure mode published
in the MHS research preview: viscous protein solutions foam at high flow rates
or with aggressive mixing, liquid level detection then hits foam, and bubbles
distort optical readings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROWS = "ABCDEFGH"
COLS = range(1, 13)
VISCOUS_THRESHOLD_UG_ML = 25.0
FOAM_FLOW_UL_S = 50.0
MAX_GENTLE_MIX = 3


@dataclass
class Well:
    volume_ul: float = 0.0
    conc_ug_ml: float = 0.0
    liquid: str = "empty"        # empty, aqueous, dye, viscous_protein
    foam: bool = False
    capacity_ul: float = 300.0

    @property
    def viscous(self) -> bool:
        return self.liquid == "viscous_protein" and self.conc_ug_ml > VISCOUS_THRESHOLD_UG_ML


@dataclass
class Plate:
    plate_id: str
    wells: dict[str, Well]
    location: str


@dataclass
class LabWorld:
    plates: dict[str, Plate] = field(default_factory=dict)
    online: dict[str, bool] = field(default_factory=dict)
    readings: dict[str, dict[str, float]] = field(default_factory=dict)
    transfers: list[dict] = field(default_factory=list)          # every transfer the hardware performed
    foam_events: list[tuple[int, str]] = field(default_factory=list)   # (transfer index, well)
    moves: list[dict] = field(default_factory=list)
    reads: list[dict] = field(default_factory=list)

    @classmethod
    def default(cls) -> "LabWorld":
        w = cls()
        w.plates["RES"] = Plate("RES", {
            "DYE": Well(20000.0, 200.0, "dye", capacity_ul=20000.0),
            "BSA": Well(20000.0, 200.0, "viscous_protein", capacity_ul=20000.0),
            "BUF": Well(20000.0, 0.0, "aqueous", capacity_ul=20000.0),
        }, "lh-01")
        w.plates["P1"] = Plate("P1", {f"{r}{c}": Well() for r in ROWS for c in COLS}, "lh-01")
        return w

    def well(self, loc: str) -> Well:
        plate_id, well_id = loc.split(":")
        return self.plates[plate_id].wells[well_id]

    def has(self, loc: str) -> bool:
        try:
            plate_id, well_id = loc.split(":")
            return well_id in self.plates[plate_id].wells
        except (KeyError, ValueError):
            return False
