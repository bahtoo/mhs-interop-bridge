"""Vendor service: the device maker's remote service desk, as an application on MHS.

In a hospital, analyzers are already connected to the LIS and the EHR. What still takes people and downtime is
operating them: maintenance, calibration, troubleshooting, consumables, and the call between a remote fix and a
field engineer. The vendor's remote service owns most of that, and stays the regulated manufacturer while
doing it. This app is that desk.

* A **service case** is the vendor's record. Opening one captures a diagnostics bundle read through MHS: sensors
  against their normal ranges, the event log, reagents and calibrations.
* Remote fixes (maintenance, calibration) are ordinary device procedures, called through MHS like anything else.
* Some fixes need **hands**: clearing a blocked probe, loading a reagent pack. Those are requested as a
  ``work_instruction``: a draft in the review queue. A named technician does the work and confirms it. Only that
  confirmation changes anything physical; an agent can never perform a hands-on step itself.
* A case closes only when the device is back in range and QC passes.
"""

from __future__ import annotations

import itertools
from typing import Any

from ..mhs_shim import Decision, Manifest, Param, Procedure, State
from .base import validate
from .clinical.qc import westgard, z_scores
from .operator import content_hash


class ServiceApp:
    app_id = "service"
    REVIEWED = {"request_work": "work_instruction"}
    NEEDS_HUMAN = set(REVIEWED)

    def __init__(self, runtime: Any, operator: Any):
        self.runtime, self.operator = runtime, operator
        self.cases: dict[str, dict] = {}
        self._ids = itertools.count(1)
        operator.attach(self)

    @property
    def actor(self) -> str:
        return f"app:{self.app_id}"

    def manifest(self) -> Manifest:
        return Manifest(self.app_id, "application", "mhs-interop-bridge", "vendor service desk",
                        "MHS client (device operations)", [
            State("cases", "list", "service cases with their status"),
            State("case:<case_id>", "object", "one case: diagnostics captured at opening, actions, resolution")], [
            Procedure("open_case", "Open a service case for a device. Captures sensors, the event log, reagents "
                      "and calibrations as the diagnostics bundle.",
                      [Param("device_id", "string", "device"), Param("summary", "string", "the reported problem")]),
            Procedure("request_work", "Ask a technician at the instrument to do a hands-on task. Drafted for review: "
                      "the technician does the work and confirms it, and only then is it recorded as done.",
                      [Param("case_id", "string", "service case"), Param("device_id", "string", "device"),
                       Param("task", "string", "a hands-on task from the device's maintenance_tasks"),
                       Param("reason", "string", "why, with the evidence")],
                      safety_checks=["the task is a hands-on task the device lists"]),
            Procedure("close_case", "Close a case once the device is back in service.",
                      [Param("case_id", "string", "service case"), Param("resolution", "string", "what fixed it")],
                      safety_checks=["every sensor within its normal range", "latest QC passes for every test"])],
            ["A case is the vendor's record of what was seen, done and by whom.",
             "Hands-on work is done by a person at the instrument; request it and wait for their confirmation."])

    def read(self, state: str) -> Any:
        if state == "cases":
            return [{k: c[k] for k in ("case_id", "device_id", "status", "summary")} for c in self.cases.values()]
        if state.startswith("case:"):
            return self.cases[state.split(":", 1)[1]]
        raise KeyError(state)

    # --- gates ----------------------------------------------------------------

    def _device_ops(self, device_id: str) -> dict:
        d = self.runtime.drivers.get(device_id)
        return getattr(d, "ops", {}) if d is not None else {}

    def _qc_failures(self, device_id: str) -> list[str]:
        targets = self.runtime.read(device_id, "qc_targets", actor=self.actor)
        bad = []
        for test, levels in (targets.items() if isinstance(targets, dict) else []):
            hist = self.runtime.read(device_id, f"qc_history:{test}", actor=self.actor)
            if westgard(z_scores(hist, levels)).status not in ("pass", "warn"):
                bad.append(test)
        return bad

    def check(self, procedure: str, args: dict) -> Decision:
        if procedure == "open_case":
            if not self._device_ops(args["device_id"]):
                return Decision.block("no_service_access", f"{args['device_id']} exposes no service operations")
            return Decision.ok()
        case = self.cases.get(args.get("case_id", ""))
        if case is None:
            return Decision.block("unknown_case", f"no case {args.get('case_id')}")
        if case["status"] != "open":
            return Decision.block("case_closed", f"case {case['case_id']} is {case['status']}")
        if procedure == "request_work":
            if args["device_id"] != case["device_id"]:
                return Decision.block("wrong_device", f"case {case['case_id']} is for {case['device_id']}")
            if args["task"] not in self._device_ops(args["device_id"]).get("hands_on_tasks", {}):
                return Decision.block("not_hands_on", f"{args['task']} is not a hands-on task on {args['device_id']}",
                                      "Remote tasks run through run_maintenance on the device.")
            return Decision.ok()
        if procedure == "close_case":
            dev = case["device_id"]
            sensors = self.runtime.read(dev, "sensors", actor=self.actor)
            out = [n for n, s in sensors.items() if not s["normal"][0] <= s["value"] <= s["normal"][1]]
            if out:
                return Decision.block("not_recovered", f"{dev} sensors out of range: {out}")
            failing = self._qc_failures(dev)
            if failing:
                return Decision.block("qc_not_passing", f"latest QC on {dev} does not pass for {failing}",
                                      "Run QC after the fix; a case closes on evidence, not on the fix alone.")
            return Decision.ok()
        return Decision.block("unknown_procedure", f"{self.app_id} has no procedure {procedure!r}")

    # --- invoke -----------------------------------------------------------------

    def invoke(self, procedure: str, args: dict, actor: str = "agent", rationale: str = "") -> dict:
        d, draft = self.prepare(procedure, args, actor, rationale)
        if not d.allowed:
            self.runtime.record(actor, "app", self.app_id, procedure, args, "blocked", d.rule, d.reason)
            return {"status": "blocked", "rule": d.rule, "reason": d.reason, "hint": d.hint}
        if procedure in self.REVIEWED:
            rid = self.operator.request(self.app_id, procedure, args, actor, draft)
            self.runtime.record(actor, "app", self.app_id, procedure, args, "needs_approval", reason=rid)
            req = self.operator.approvals[rid]
            return {"status": "needs_approval", "request_id": rid, "kind": req.kind,
                    "payload_sha256": req.payload_sha256,
                    "reason": "a technician at the instrument does the work and confirms it; poll "
                              "operator_approval_status"}
        result = self._record(procedure, args, actor)
        self.runtime.record(actor, "app", self.app_id, procedure, args, "executed")
        return result

    def prepare(self, procedure: str, args: dict, actor: str = "agent", rationale: str = "",
                prior: Any = None) -> tuple[Decision, dict]:
        d = validate(self.manifest(), procedure, args) or self.check(procedure, args)
        if not d.allowed or procedure not in self.REVIEWED:
            return d, {}
        task = self._device_ops(args["device_id"])["hands_on_tasks"][args["task"]]
        steps = list(task.get("steps", []))
        content = {"device_id": args["device_id"], "task": args["task"], "steps": steps}
        return d, {
            "kind": "work_instruction",
            "intent": {"source": {"type": "ServiceCase", "id": args["case_id"], "text": args["reason"],
                                  "raw": {"summary": self.cases[args["case_id"]]["summary"]}},
                       "agent_rationale": rationale},
            "proposed": [{"path": f"{args['device_id']}.{args['task']}.step[{i}]", "value": s,
                          "from": f"device profile operations.hands_on_tasks.{args['task']}"}
                         for i, s in enumerate(steps)],
            "payload": {"format": "work_instruction", "content": content,
                        "text": f"{task['description']} (about {task.get('minutes', '?')} min)\n"
                                + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
                                + "\nConfirm here when done."},
            "gates": {"allowed": True, "checked": ["the task is a hands-on task the device lists"]},
        }

    def execute_approved(self, procedure: str, args: dict, approver: str, draft: Any) -> dict:
        """The approver is the technician confirming the work is done. This is the only path to a physical change."""
        self.operator.authorise(draft)
        d = self.check(procedure, args)
        if not d.allowed:
            return {"status": "blocked", "rule": d.rule, "reason": d.reason}
        _, fresh = self.prepare(procedure, args, draft.requested_by)
        if content_hash(fresh["payload"]["content"]) != draft.payload_sha256:
            return {"status": "blocked", "rule": "payload_changed", "reason": "the work instruction changed"}
        result = self.runtime.drivers[args["device_id"]].perform_by_hand(args["task"], approver)
        self.cases[args["case_id"]]["actions"].append({"hands_on": args["task"], "by": approver,
                                                       "request_id": draft.request_id})
        self.runtime.record(approver, "approval", self.app_id, procedure, args, "executed")
        return result

    def _record(self, procedure: str, args: dict, actor: str) -> dict:
        if procedure == "open_case":
            dev = args["device_id"]
            case_id = f"case-{next(self._ids):04d}"
            diag = {s: self.runtime.read(dev, s, actor=self.actor) for s in ("sensors", "event_log", "reagents")}
            self.cases[case_id] = {"case_id": case_id, "device_id": dev, "summary": args["summary"], "status": "open",
                                   "opened_by": actor, "diagnostics": diag, "actions": [], "resolution": ""}
            return {"status": "ok", "case_id": case_id, "diagnostics": diag}
        case = self.cases[args["case_id"]]
        case.update(status="closed", resolution=args["resolution"], closed_by=actor)
        return {"status": "ok", "case_id": case["case_id"], "closed": True}
