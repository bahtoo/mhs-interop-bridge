"""Reusable driver interlocks.

An interlock is device knowledge a driver applies on the write path: a veto
before a call that is physically unsafe (``precheck``), and learning from what
the device reported (``after``). They are configured per device in its profile,
so the same interlock serves any instrument with the same failure mode.

They live in drivers, not in apps, because they are about the hardware. A
liquid handler should refuse to aspirate from a well it just reported foam in,
whoever is asking and whatever application is running.
"""

from __future__ import annotations

from typing import Any

from ..mhs_shim import Decision

PHYSICAL_HINT = ("This is a physical failure, not a software error. Retrying in the same place will not fix it; "
                 "move to a clean position or change the physical conditions.")


class Interlock:
    name = "interlock"
    describes = ""

    def precheck(self, driver: Any, procedure: str, args: dict) -> Decision | None:
        return None

    def after(self, driver: Any, procedure: str, args: dict, result: dict) -> None:
        return None


class QuarantineOnError(Interlock):
    """After the device reports ``error`` for a position, refuse any further call
    that touches that position. Encodes 'do not retry harder on a physical failure'."""

    name = "quarantine_on_error"

    def __init__(self, error: str, params: list[str], detail_key: str, hint: str = PHYSICAL_HINT):
        self.error, self.params, self.detail_key, self.hint = error, params, detail_key, hint
        self.quarantined: set[str] = set()
        self.describes = f"positions reported in {error} are quarantined for {', '.join(params)}"

    def precheck(self, driver, procedure, args):
        for p in self.params:
            if args.get(p) in self.quarantined:
                return Decision.block("quarantine", f"{args[p]} is quarantined after {self.error}", self.hint)
        return None

    def after(self, driver, procedure, args, result):
        if result.get("error") == self.error and result.get(self.detail_key):
            self.quarantined.add(result[self.detail_key])


class CapacityGuard(Interlock):
    """Refuse a transfer that would overfill the target well. The hardware will not stop it."""

    name = "capacity"

    def __init__(self, target: str, volume: str, capacity_ul: float):
        self.target, self.volume, self.capacity = target, volume, capacity_ul
        self.describes = f"{target} volume after the call must not exceed {capacity_ul} uL"

    def precheck(self, driver, procedure, args):
        if self.target not in args or self.volume not in args:
            return None
        plate, well = args[self.target].split(":") if ":" in args[self.target] else (args[self.target], "")
        current = driver.read("PlateMap").get(plate, {}).get(well, {}).get("volume_ul", 0.0)
        if plate != "RES" and current + float(args[self.volume]) > self.capacity:
            return Decision.block("capacity", f"{args[self.target]} would hold {current + float(args[self.volume])} uL, "
                                              f"capacity is {self.capacity} uL")
        return None


class OnDeckGuard(Interlock):
    """Refuse a call that names labware not currently at this device."""

    name = "on_deck"

    def __init__(self, params: list[str]):
        self.params = params
        self.describes = f"labware in {', '.join(params)} must be on this deck"

    def precheck(self, driver, procedure, args):
        on_deck = set(driver.read("DeckLayout").get("labware", []))
        for p in self.params:
            if p in args and args[p].split(":")[0] not in on_deck:
                return Decision.block("plate_location", f"{args[p].split(':')[0]} is not on {driver.device_id}",
                                      "Move the plate here first.")
        return None


REGISTRY = {"quarantine_on_error": QuarantineOnError, "capacity": CapacityGuard, "on_deck": OnDeckGuard}


def build(configs: list[dict]) -> list[Interlock]:
    out = []
    for cfg in configs or []:
        cfg = dict(cfg)
        out.append(REGISTRY[cfg.pop("type")](**cfg))
    return out
