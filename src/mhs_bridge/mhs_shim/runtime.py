"""Stand-in MHS runtime: registry, discovery, read, invoke, limits, audit.

What it does, each item tied to the public MHS description:

* registry and discovery of drivers                        (devices are discoverable)
* ``read`` and ``invoke``                                   (read and write primitives)
* parameter ranges from the manifest enforced on invoke     (device-level safety limits)
* routines that expand into individually checked steps      (code files chaining commands)
* emergency stop: any actor engages, only a human clears    (PROPOSAL)
* risk class ``patient_connected`` refuses every invoke     (PROPOSAL)
* driver interlocks: a driver may veto a call it knows to be physically unsafe,
  and learn from device errors (for example quarantine a well after foam)
                                                            (PROPOSAL, implemented by drivers)
* an audit record of every read and invoke

Domain rules (clinical release, research data integrity) are deliberately not
here. They belong to applications that consume MHS, see ``mhs_bridge.apps``.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .manifest import Manifest


@dataclass
class Decision:
    allowed: bool
    rule: str = ""
    reason: str = ""
    hint: str = ""

    @classmethod
    def ok(cls) -> "Decision":
        return cls(True)

    @classmethod
    def block(cls, rule: str, reason: str, hint: str = "") -> "Decision":
        return cls(False, rule, reason, hint)


@runtime_checkable
class Driver(Protocol):
    """What a driver provides. Optional hooks: ``precheck``, ``after``, ``expand``."""

    device_id: str

    @property
    def online(self) -> bool: ...

    def manifest(self) -> Manifest: ...

    def read(self, state: str) -> Any: ...

    def write(self, procedure: str, args: dict) -> dict: ...


@dataclass
class AuditEntry:
    seq: int
    ts: str
    actor: str
    kind: str
    target: str
    action: str
    args: dict
    outcome: str
    rule: str = ""
    reason: str = ""


class Runtime:
    def __init__(self, audit_path: str | Path | None = None):
        self.drivers: dict[str, Any] = {}
        self.estop = False
        self.audit: list[AuditEntry] = []
        self.audit_path = Path(audit_path) if audit_path else None
        self._seq = itertools.count(1)

    # --- registry and discovery ------------------------------------------

    def register(self, driver: Any) -> None:
        if not isinstance(driver, Driver):
            raise TypeError(f"{driver!r} does not implement the driver contract")
        self.drivers[driver.device_id] = driver

    def discover(self) -> list[dict]:
        # PROPOSAL: the emergency stop is part of every device's discovery record. An agent should be able to see
        # that the lab is stopped before it plans, not only learn it from the first refused call.
        out = []
        for dev_id, d in self.drivers.items():
            m = d.manifest()
            out.append({"device_id": dev_id, "device_type": m.device_type, "risk_class": m.risk_class,
                        "vendor": m.vendor, "model": m.model, "interface": m.interface, "online": d.online,
                        "emergency_stop": self.estop})
        return out

    def manifest(self, device_id: str) -> Manifest:
        return self.drivers[device_id].manifest()

    def describe(self, device_id: str) -> str:
        ref = self.manifest(device_id).reference()
        if self.estop:   # PROPOSAL, as in discover()
            first, _, rest = ref.partition("\n")
            ref = (f"{first}\n\n> EMERGENCY STOP ENGAGED. Every invoke on every device is refused until a human "
                   f"clears it.\n{rest}")
        return ref

    # --- read ----------------------------------------------------------------

    def read(self, device_id: str, state: str, actor: str = "agent") -> Any:
        d = self.drivers.get(device_id)
        if d is None:
            self._log(actor, "read", device_id, state, {}, "error", reason="unknown device")
            return {"error": f"unknown device {device_id}"}
        if not d.online and state != "status":
            self._log(actor, "read", device_id, state, {}, "error", reason="offline")
            return {"error": f"{device_id} is offline"}
        try:
            value = d.read(state)
        except KeyError:
            self._log(actor, "read", device_id, state, {}, "error", reason="unknown state")
            return {"error": f"unknown state {state!r} on {device_id}"}
        self._log(actor, "read", device_id, state, {}, "ok")
        return value

    # --- invoke ----------------------------------------------------------------

    def check(self, device_id: str, procedure: str, args: dict) -> Decision:
        d = self.drivers.get(device_id)
        if d is None:
            return Decision.block("unknown_device", f"no device {device_id}")
        m = d.manifest()
        if m.risk_class == "patient_connected":
            return Decision.block("read_only_device", f"{device_id} is patient-connected; nothing may be invoked on it",
                                  "Read its states. Settings and alarms are managed at the device, not through MHS.")
        if self.estop:
            return Decision.block("emergency_stop", "emergency stop is engaged; every invoke is refused",
                                  "Escalate to a human operator. Only a human can clear the stop.")
        proc = m.procedure(procedure)
        if proc is None:
            return Decision.block("unknown_procedure", f"{device_id} has no procedure {procedure!r}")
        for prm in proc.params:
            if prm.required and prm.name not in args:
                return Decision.block("missing_param", f"missing required parameter {prm.name!r}")
            v = args.get(prm.name)
            if prm.allowed and v is not None and str(v) not in prm.allowed:
                return Decision.block("limit", f"{prm.name}={v!r} is not one of {prm.allowed}")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if prm.min is not None and v < prm.min:
                    return Decision.block("limit", f"{prm.name}={v} below enforced minimum {prm.min} {prm.unit}".strip())
                if prm.max is not None and v > prm.max:
                    return Decision.block("limit", f"{prm.name}={v} above enforced maximum {prm.max} {prm.unit}".strip(),
                                          "Split the operation or choose a value inside the enforced range.")
        if not d.online:
            return Decision.block("device_unreachable", f"{device_id} is offline or unreachable",
                                  "Do not continue with steps that depend on this device. Escalate.")
        pre = getattr(d, "precheck", None)
        if pre:
            decision = pre(procedure, args)
            if decision is not None and not decision.allowed:
                return decision
        return Decision.ok()

    def invoke(self, device_id: str, procedure: str, args: dict | None = None, actor: str = "agent") -> dict:
        args = dict(args or {})
        decision = self.check(device_id, procedure, args)
        if not decision.allowed:
            self._log(actor, "invoke", device_id, procedure, args, "blocked", decision.rule, decision.reason)
            return {"status": "blocked", "rule": decision.rule, "reason": decision.reason, "hint": decision.hint}
        d = self.drivers[device_id]
        expand = getattr(d, "expand", None)
        steps = expand(procedure, args) if expand else None
        if steps is not None:
            return self._routine(device_id, procedure, args, steps, actor)
        result = d.write(procedure, args)
        after = getattr(d, "after", None)
        if after:
            after(procedure, args, result)
        self._log(actor, "invoke", device_id, procedure, args,
                  "executed" if result.get("status") == "ok" else "error", reason=result.get("error", ""))
        return result

    def _routine(self, device_id: str, procedure: str, args: dict, steps: list[tuple[str, dict]],
                 actor: str) -> dict:
        self._log(actor, "invoke", device_id, procedure, args, "executed", reason=f"routine of {len(steps)} steps")
        for i, (proc, step_args) in enumerate(steps):
            res = self.invoke(device_id, proc, step_args, actor=f"{actor}/routine")
            if res.get("status") != "ok":
                return {"status": res.get("status"), "completed_steps": i, "total_steps": len(steps),
                        "failed_step": {"procedure": proc, **step_args},
                        **{k: v for k, v in res.items() if k != "status"}}
        return {"status": "ok", "completed_steps": len(steps), "total_steps": len(steps)}

    # --- emergency stop ------------------------------------------------------------

    def engage_estop(self, actor: str, reason: str) -> dict:
        self.estop = True
        self._log(actor, "estop", "mhs", "engage", {"reason": reason}, "executed")
        return {"status": "ok", "estop": True}

    def clear_estop(self, operator: str) -> None:
        """Human-only: called from the operator channel, never from an agent tool."""
        self.estop = False
        self._log(operator, "estop", "mhs", "clear", {}, "executed")

    # --- audit -------------------------------------------------------------------

    def _log(self, actor: str, kind: str, target: str, action: str, args: dict, outcome: str,
             rule: str = "", reason: str = "") -> None:
        entry = AuditEntry(next(self._seq), datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           actor, kind, target, action, args, outcome, rule, reason)
        self.audit.append(entry)
        if self.audit_path:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(vars(entry), default=str) + "\n")

    def record(self, actor: str, kind: str, target: str, action: str, args: dict, outcome: str,
               rule: str = "", reason: str = "") -> None:
        """Let applications add to the same audit trail (kind 'app' or 'ehr')."""
        self._log(actor, kind, target, action, args, outcome, rule, reason)

    def summary(self) -> dict:
        inv = [e for e in self.audit if e.kind == "invoke"]
        return {"invokes": len(inv), "executed": sum(e.outcome == "executed" for e in inv),
                "blocked": sum(e.outcome == "blocked" for e in inv),
                "device_errors": sum(e.outcome == "error" for e in inv)}
