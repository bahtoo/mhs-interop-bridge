"""Routines: chained driver commands, the MHS 'code file' idea.

A routine expands into ordinary procedure calls. The runtime runs each step
through the same checks as a direct call and stops at the first failure, so a
routine makes long sequences fast without making them less safe.
"""

from __future__ import annotations

from ..mhs_shim import Param


class SerialDilution:
    description = ("Fill a row with a 2-fold dilution series. Column 1 gets TopFraction of stock made up to "
                   "200 uL with buffer; columns 2..Steps get 100 uL buffer then 100 uL from the previous column.")

    @staticmethod
    def params(driver) -> list[Param]:
        transfer = next(c for f in driver.features for c in f.commands if c.identifier == "Transfer")
        lim = {p.identifier: p.data_type for p in transfer.parameters}
        flow, mix = lim["FlowRate"], lim["MixCycles"]
        return [Param("Plate", "string", "target plate"),
                Param("Row", "string", "row letter", allowed=list("ABCDEFGH")),
                Param("Stock", "string", "reservoir position, for example RES:DYE or RES:BSA"),
                Param("TopFraction", "number", "fraction of stock in column 1", 0.05, 1.0),
                Param("Steps", "integer", "number of columns", 2, 12),
                Param("FlowRate", "number", "flow rate for every step", flow.minimum, flow.maximum, flow.unit),
                Param("MixCycles", "integer", "mix cycles per step", mix.minimum, mix.maximum)]

    @staticmethod
    def expand(a: dict) -> list[tuple[str, dict]]:
        plate, row, stock = a["Plate"], a["Row"], a["Stock"]
        f, steps, flow, mix = float(a["TopFraction"]), int(a["Steps"]), float(a["FlowRate"]), int(a["MixCycles"])
        plan: list[tuple[str, dict]] = []
        if f < 1.0:
            plan.append(("Transfer", {"Source": "RES:BUF", "Target": f"{plate}:{row}1", "Volume": round(200 * (1 - f), 3),
                                      "FlowRate": flow, "MixCycles": 0}))
        plan.append(("Transfer", {"Source": stock, "Target": f"{plate}:{row}1", "Volume": round(200 * f, 3),
                                  "FlowRate": flow, "MixCycles": mix}))
        for c in range(2, steps + 1):
            plan.append(("Transfer", {"Source": "RES:BUF", "Target": f"{plate}:{row}{c}", "Volume": 100.0,
                                      "FlowRate": flow, "MixCycles": 0}))
            plan.append(("Transfer", {"Source": f"{plate}:{row}{c - 1}", "Target": f"{plate}:{row}{c}", "Volume": 100.0,
                                      "FlowRate": flow, "MixCycles": mix}))
        return plan


REGISTRY = {"SerialDilution": SerialDilution}
