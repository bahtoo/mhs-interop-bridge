"""Generic SiLA 2 to MHS driver.

Give it the instrument's SiLA 2 Feature Definitions and a client, and it
produces an MHS driver:

    SiLA 2 Command            -> MHS procedure (same identifier)
    Command Parameter         -> procedure parameter
    MinimalInclusive / MaximalInclusive / Unit / Set
                              -> enforced range, unit, allowed values
    Defined Execution Error   -> documented procedure error, with a hint
    Property                  -> readable state (JSON strings decoded)

What the Feature Definition cannot say, the device profile adds: operating
knowledge as tags, hints for each defined error, interlocks, routines, and the
risk class. That split is deliberate. The FDL describes the interface; the
profile describes how an experienced scientist uses the instrument.

The client is anything with ``call_command(feature, command, params)`` and
``get_property(feature, property)``. Here that is an in-process simulator; in a
lab it is a SiLA 2 gRPC client stub for the same Feature Definitions.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Callable

from ..mhs_shim import Manifest, Param, Procedure, State
from ..protocols.sila2 import Feature, SiLA2ExecutionError, parse_feature
from . import interlocks as il
from . import routines as rt

_TYPE = {"String": "string", "Integer": "integer", "Real": "number", "Boolean": "boolean"}


def load_builtin_feature(filename: str) -> Feature:
    return parse_feature((resources.files("mhs_bridge") / "features" / filename).read_text(encoding="utf-8"))


class SiLA2Driver:
    def __init__(self, profile: dict, client: Any, features: list[Feature],
                 is_online: Callable[[], bool] = lambda: True):
        self.profile = profile
        self.device_id = profile["device_id"]
        self.client = client
        self.features = features
        self._is_online = is_online
        self.interlocks = il.build(profile.get("interlocks", []))
        self.routines = {name: rt.REGISTRY[name] for name in profile.get("routines", [])}
        self._command_feature = {c.identifier: f.identifier for f in features for c in f.commands}
        self._property_feature = {p.identifier: f.identifier for f in features for p in f.properties}

    @property
    def online(self) -> bool:
        return self._is_online()

    # --- manifest ----------------------------------------------------------

    def manifest(self) -> Manifest:
        hints = self.profile.get("error_hints", {})
        procedures: list[Procedure] = []
        for f in self.features:
            for c in f.commands:
                params = [Param(p.identifier, ("list of " if p.data_type.is_list else "") + _TYPE.get(p.data_type.basic, "string"),
                                p.description, p.data_type.minimum, p.data_type.maximum, p.data_type.unit,
                                allowed=p.data_type.allowed) for p in c.parameters]
                errors = {e: (f.errors.get(e, "") + (" " + hints[e] if e in hints else "")).strip() for e in c.errors}
                checks = [i.describes for i in self.interlocks if i.describes]
                procedures.append(Procedure(c.identifier, f"{c.description} (SiLA 2 {f.fully_qualified})",
                                            params, errors, checks))
        for name, routine in self.routines.items():
            procedures.append(Procedure(name, routine.description, routine.params(self), routine=True,
                                        safety_checks=["every step is checked individually; the routine stops at the first failure"]))
        states = [State("status", "string", "online or offline")]
        for f in self.features:
            states += [State(p.identifier, "object" if p.data_type.basic == "String" else _TYPE.get(p.data_type.basic, "string"),
                             p.description) for p in f.properties]
        return Manifest(self.device_id, self.profile["device_type"], self.profile["vendor"], self.profile["model"],
                        "SiLA 2 (" + ", ".join(f.identifier for f in self.features) + ")", states, procedures,
                        self.profile.get("tags", []), self.profile.get("risk_class", ""), driver="sila2")

    # --- read and write ------------------------------------------------------

    def read(self, state: str) -> Any:
        if state == "status":
            return "online" if self.online else "offline"
        feature = self._property_feature[state]
        raw = self.client.get_property(feature, state)
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    def write(self, procedure: str, args: dict) -> dict:
        feature = self._command_feature.get(procedure)
        if feature is None:
            raise KeyError(procedure)
        try:
            response = self.client.call_command(feature, procedure, args)
        except SiLA2ExecutionError as exc:
            out = {"status": "error", "error": exc.identifier, "detail": exc.message, **exc.details}
            hint = self.profile.get("error_hints", {}).get(exc.identifier)
            if hint:
                out["hint"] = hint
            return out
        return {"status": "ok", **response}

    # --- hooks used by the runtime ---------------------------------------------

    def precheck(self, procedure: str, args: dict):
        if procedure in self.routines:
            return None
        for lock in self.interlocks:
            d = lock.precheck(self, procedure, args)
            if d is not None and not d.allowed:
                return d
        return None

    def after(self, procedure: str, args: dict, result: dict) -> None:
        for lock in self.interlocks:
            lock.after(self, procedure, args, result)

    def expand(self, procedure: str, args: dict):
        routine = self.routines.get(procedure)
        return routine.expand(args) if routine else None
