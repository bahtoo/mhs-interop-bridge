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
* Some faults need an engineer and a part. ``dispatch_engineer`` drafts the field-service visit for a named
  service lead to approve, with the logged fault as evidence; vendor policy is to try a remote reset first when the
  fault catalogue says a reset can clear it.
* A case closes only when the device is back in range and QC passes.

Hospital staff use ``request_work`` too, without a case, for their own start-of-day work at the bench.
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
    REVIEWED = {"request_work": "work_instruction", "dispatch_engineer": "field_service"}
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
                      [Param("device_id", "string", "device"),
                       Param("task", "string", "a hands-on task from the device's maintenance_tasks"),
                       Param("reason", "string", "why, with the evidence"),
                       Param("case_id", "string", "service case, if the work is part of one", required=False)],
                      safety_checks=["the task is a hands-on task the device lists"]),
            Procedure("dispatch_engineer", "Request a field engineer with a part. Drafted for a named service lead's "
                      "approval. The analyzer stays out of service until the engineer has been.",
                      [Param("case_id", "string", "service case"), Param("part", "string", "the part to bring"),
                       Param("evidence", "string", "the fault codes and readings that justify the visit")],
                      safety_checks=["the part is the field-service part for a fault the device logged",
                                     "a remote reset was tried first where the fault catalogue says to"]),
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
        if procedure == "request_work" and not args.get("case_id"):
            return self._check_task(args)
        case = self.cases.get(args.get("case_id", ""))
        if case is None:
            return Decision.block("unknown_case", f"no case {args.get('case_id')}")
        if case["status"] != "open":
            return Decision.block("case_closed", f"case {case['case_id']} is {case['status']}")
        if procedure == "request_work":
            if args["device_id"] != case["device_id"]:
                return Decision.block("wrong_device", f"case {case['case_id']} is for {case['device_id']}")
            return self._check_task(args)
        if procedure == "dispatch_engineer":
            dev = case["device_id"]
            catalogue = self._device_ops(dev).get("field_service", {})
            log = self.runtime.read(dev, "event_log", actor=self.actor)
            logged = [e["code"] for e in log]
            code = next((c for c, f in catalogue.items() if f["part"] == args["part"] and c in logged), None)
            if code is None:
                return Decision.block("part_not_indicated", f"{args['part']!r} is not the field-service part for any "
                                      f"fault {dev} logged", "Read the device's field_service catalogue and event log.")
            if catalogue[code].get("try_reset"):
                first = logged.index(code)
                if "R-001" not in logged[first:]:
                    return Decision.block("remote_first", f"{code} can be transient; no remote reset since it was "
                                          "logged", "Reset the analyzer once (reset_analyzer); dispatch if it comes back.")
            if case.get("dispatch"):
                return Decision.block("already_dispatched", f"case {case['case_id']} already has an engineer booked")
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

    def _check_task(self, args: dict) -> Decision:
        if args["task"] not in self._device_ops(args["device_id"]).get("hands_on_tasks", {}):
            return Decision.block("not_hands_on", f"{args['task']} is not a hands-on task on {args['device_id']}",
                                  "Remote tasks run through run_maintenance on the device.")
        return Decision.ok()

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
        if procedure == "dispatch_engineer":
            return d, self._draft_dispatch(args, rationale)
        task = self._device_ops(args["device_id"])["hands_on_tasks"][args["task"]]
        steps = list(task.get("steps", []))
        content = {"device_id": args["device_id"], "task": args["task"], "steps": steps}
        return d, {
            "kind": "work_instruction",
            "intent": {"source": {"type": "ServiceCase" if args.get("case_id") else "LabWork",
                                  "id": args.get("case_id", args["device_id"]), "text": args["reason"],
                                  "raw": {"summary": self.cases[args["case_id"]]["summary"]}
                                  if args.get("case_id") else {}},
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

    def _draft_dispatch(self, args: dict, rationale: str) -> dict:
        case = self.cases[args["case_id"]]
        content = {"case_id": case["case_id"], "device_id": case["device_id"], "part": args["part"]}
        return {
            "kind": "field_service",
            "intent": {"source": {"type": "ServiceCase", "id": case["case_id"], "text": args["evidence"],
                                  "raw": {"summary": case["summary"]}}, "agent_rationale": rationale},
            "proposed": [{"path": f"{case['device_id']}.field_service.part", "value": args["part"],
                          "from": "device profile operations.field_service"}],
            "payload": {"format": "field_service", "content": content,
                        "text": f"Field engineer to {case['device_id']} with {args['part']}.\nEvidence: "
                                f"{args['evidence']}\nThe analyzer stays out of service until the visit."},
            "gates": {"allowed": True, "checked": ["the part matches a logged fault", "remote reset tried first"]},
        }

    def execute_approved(self, procedure: str, args: dict, approver: str, draft: Any) -> dict:
        """For a work instruction the approver is the technician confirming the work is done: the only path to a
        physical change. For a dispatch it is the service lead booking the visit; nothing physical changes."""
        self.operator.authorise(draft)
        d = self.check(procedure, args)
        if not d.allowed:
            return {"status": "blocked", "rule": d.rule, "reason": d.reason}
        _, fresh = self.prepare(procedure, args, draft.requested_by)
        if content_hash(fresh["payload"]["content"]) != draft.payload_sha256:
            return {"status": "blocked", "rule": "payload_changed", "reason": "the request changed"}
        self.runtime.record(approver, "approval", self.app_id, procedure, args, "executed")
        if procedure == "dispatch_engineer":
            case = self.cases[args["case_id"]]
            case["dispatch"] = {"part": args["part"], "approved_by": approver, "request_id": draft.request_id}
            case["actions"].append({"dispatch": args["part"], "by": approver, "request_id": draft.request_id})
            return {"status": "ok", "case_id": case["case_id"], "dispatched": args["part"], "approved_by": approver}
        result = self.runtime.drivers[args["device_id"]].perform_by_hand(args["task"], approver)
        if args.get("case_id"):
            self.cases[args["case_id"]]["actions"].append({"hands_on": args["task"], "by": approver,
                                                           "request_id": draft.request_id})
        return result

    def _record(self, procedure: str, args: dict, actor: str) -> dict:
        if procedure == "open_case":
            dev = args["device_id"]
            case_id = f"case-{next(self._ids):04d}"
            diag = {s: self.runtime.read(dev, s, actor=self.actor) for s in ("mode", "sensors", "event_log", "reagents")}
            self.cases[case_id] = {"case_id": case_id, "device_id": dev, "summary": args["summary"], "status": "open",
                                   "opened_by": actor, "diagnostics": diag, "actions": [], "resolution": ""}
            return {"status": "ok", "case_id": case_id, "diagnostics": diag}
        case = self.cases[args["case_id"]]
        case.update(status="closed", resolution=args["resolution"], closed_by=actor)
        return {"status": "ok", "case_id": case["case_id"], "closed": True}
