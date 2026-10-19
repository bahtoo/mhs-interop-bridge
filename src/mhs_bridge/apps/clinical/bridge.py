"""Clinical bridge: an application that connects MHS devices to an EHR over FHIR.

It sits outside the MHS boundary and uses MHS the way any client does: it
discovers analyzers, reads their catalogues and results, and invokes
``download_order`` on them. On top of that it owns the clinical semantics that
are not a hardware concern:

* FHIR ServiceRequest intake and capability routing;
* the order lifecycle, mirrored to the EHR as FHIR Task;
* release gates: holds, patient identity against the order, order state,
  Westgard QC, critical-value acknowledgement;
* critical-result notification (FHIR Communication), specimen recollection (Task);
* charting of nurse-validated vital signs;
* FHIR egress (Observation, DiagnosticReport, Task, Communication).

Acts that a named human must own, acknowledging a critical value and charting
vital signs, go through the operator channel.
"""

from __future__ import annotations

import itertools
from typing import Any

from ...canonical import CanonicalResult, ReleaseState
from ...mhs_shim import Decision, Manifest, Param, Procedure, State
from ...protocols import fhir
from ..base import validate
from . import orders as order_intake
from .ehr import MemoryEhr
from .models import Order
from .qc import QCStatus, westgard, z_scores

_PHYSICAL = ("Held for a physical or identity reason. Do not re-run the same specimen; request a new one and "
             "escalate.")


class ClinicalBridgeApp:
    app_id = "clinical"

    def __init__(self, runtime: Any, operator: Any, ehr: Any | None = None):
        self.runtime, self.operator = runtime, operator
        self.ehr = ehr or MemoryEhr()
        self.orders: dict[str, Order] = {}
        self.released: set[str] = set()
        self.acks: dict[str, list[dict]] = {}
        self.charted: set[str] = set()
        self.recollections: dict[str, str] = {}
        self._index: dict[str, tuple[str, str]] = {}     # result_id -> (device_id, specimen_id)
        self._ids = itertools.count(1)
        operator.attach(self)

    @property
    def actor(self) -> str:
        return f"app:{self.app_id}"

    # ------------------------------------------------------------------
    # manifest
    # ------------------------------------------------------------------

    def manifest(self) -> Manifest:
        return Manifest(self.app_id, "application", "mhs-interop-bridge", "clinical bridge", "MHS client, FHIR R4 to the EHR", [
            State("orders", "list", "EHR orders with routing, unroutable codes and Task status"),
            State("order:<order_id>", "object", "one order and its lifecycle history"),
            State("results:<specimen_id>", "list", "canonical results with release state and hold reasons"),
            State("qc_status:<device_id>:<test>", "object", "Westgard status for a test on an analyzer"),
            State("vitals:<device_id>", "object", "the monitor's latest set, whether it can be charted, and why not"),
            State("ehr_outbox", "list", "FHIR bundles sent to the EHR (in-memory EHR only)")], [
            Procedure("dispatch_order", "Download an EHR order to the analyzers it was routed to. Task moves to accepted.",
                      [Param("order_id", "string", "ServiceRequest id")],
                      safety_checks=["order not yet dispatched", "every test routable to an online analyzer"]),
            Procedure("release_results", "Send a specimen's results to the EHR as Observations and a DiagnosticReport.",
                      [Param("specimen_id", "string", "specimen barcode")],
                      safety_checks=["no held results", "patient matches the order", "order dispatched",
                                     "latest QC passes Westgard", "critical values acknowledged"]),
            Procedure("acknowledge_critical", "Record that a clinician was told about a critical value. "
                      "Needs a human approval; sends a FHIR Communication when approved.",
                      [Param("result_id", "string", "result id"), Param("note", "string", "how they were told"),
                       Param("recipient", "string", "who was told", required=False)]),
            Procedure("request_recollection", "Ask the ward for a new specimen. Sends a FHIR Task; puts the order on hold.",
                      [Param("specimen_id", "string", "specimen barcode"), Param("reason", "string", "why")]),
            Procedure("chart_vitals", "Chart a bedside monitor's latest set as vital-signs Observations. "
                      "Needs a nurse's validation through the operator channel.",
                      [Param("device_id", "string", "patient monitor")],
                      safety_checks=["monitor associated with a patient", "no artefact or lead-off values",
                                     "set not already charted"])],
            ["Held results stay held. Do not look for a way around a gate.",
             "A patient mismatch between order and result is a potential wrong-patient specimen."])

    # ------------------------------------------------------------------
    # EHR side (not an agent tool)
    # ------------------------------------------------------------------

    def receive_order(self, service_request: dict) -> dict:
        try:
            order = order_intake.parse_service_request(service_request)
        except order_intake.OrderError as exc:
            self.runtime.record("ehr", "ehr", self.app_id, "receive_order", {"id": service_request.get("id")},
                                "error", reason=str(exc))
            return {"status": "error", "error": str(exc)}
        order_intake.route(order, self.runtime, self.actor)
        self.orders[order.order_id] = order
        self._task(order, "requested", "unroutable: " + ", ".join(order.unroutable) if order.unroutable else "")
        self.runtime.record("ehr", "ehr", self.app_id, "receive_order", {"id": order.order_id}, "ok")
        return {"status": "ok", "order_id": order.order_id, "routing": order.routing, "unroutable": order.unroutable}

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def read(self, state: str) -> Any:
        if state == "orders":
            return [o.to_dict() for o in self.orders.values()]
        if state.startswith("order:"):
            return self.orders[state.split(":", 1)[1]].to_dict()
        if state.startswith("results:"):
            return self.results(state.split(":", 1)[1])
        if state.startswith("qc_status:"):
            _, dev, test = state.split(":")
            return vars(self.qc_status(dev, test))
        if state.startswith("vitals:"):
            return self.vitals(state.split(":", 1)[1])
        if state == "ehr_outbox":
            return getattr(self.ehr, "bundles", [])
        raise KeyError(state)

    def _analyzers(self) -> list[str]:
        return [d["device_id"] for d in self.runtime.discover() if d["device_type"].endswith("analyzer")]

    def order_for(self, specimen_id: str) -> Order | None:
        return next((o for o in self.orders.values() if o.specimen_id == specimen_id), None)

    def results(self, specimen_id: str) -> list[dict]:
        """Results for a specimen as read through MHS, with this app's release view applied."""
        out: list[dict] = []
        order = self.order_for(specimen_id)
        for dev in self._analyzers():
            rows = self.runtime.read(dev, f"results:{specimen_id}", actor=self.actor)
            if not isinstance(rows, list):
                continue
            for r in rows:
                r = dict(r)
                self._index[r["result_id"]] = (dev, specimen_id)
                if order and not r["order_id"]:
                    r["order_id"] = order.order_id      # link by specimen even if the order never reached the analyzer
                holds = list(r["hold_reasons"])
                if order and r["patient_id"] != order.patient_id:
                    holds.append(f"patient_mismatch: result reports {r['patient_id'] or 'no patient'}, "
                                 f"order {order.order_id} is for {order.patient_id}")
                r["hold_reasons"] = holds
                r["acknowledgements"] = self.acks.get(r["result_id"], [])
                if r["result_id"] in self.released:
                    r["release"] = "released"
                elif holds:
                    r["release"] = "held"
                elif r["interpretation"] in ("LL", "HH") and not r["acknowledgements"]:
                    r["release"] = "pending_ack"
                else:
                    r["release"] = "releasable"
                out.append(r)
        if order and out and order.task_status == "accepted":
            self._task(order, "in-progress")
        return out

    def qc_status(self, device_id: str, test: str) -> QCStatus:
        history = self.runtime.read(device_id, f"qc_history:{test}", actor=self.actor)
        targets = self.runtime.read(device_id, "qc_targets", actor=self.actor)
        if not isinstance(history, list) or not isinstance(targets, dict) or test not in targets:
            return QCStatus("none")
        return westgard(z_scores(history, targets[test]))

    def vitals(self, device_id: str) -> dict:
        latest = self.runtime.read(device_id, "latest_vitals", actor=self.actor)
        assoc = self.runtime.read(device_id, "association", actor=self.actor)
        if not isinstance(latest, dict) or "error" in latest:
            return {"error": f"cannot read {device_id}"}
        reasons = sorted({h for r in latest["results"] for h in r["hold_reasons"]})
        if not assoc.get("patient_id"):
            reasons.append("unassociated_device: monitor is not bound to a patient")
        return {**latest, "association": assoc, "charted": latest["set_id"] in self.charted,
                "chartable": not reasons and latest["set_id"] not in self.charted, "hold_reasons": reasons}

    # ------------------------------------------------------------------
    # gates
    # ------------------------------------------------------------------

    def check(self, procedure: str, args: dict) -> Decision:
        if procedure == "dispatch_order":
            o = self.orders.get(args["order_id"])
            if o is None:
                return Decision.block("unknown_order", f"no order {args['order_id']}")
            if o.task_status != "requested":
                return Decision.block("already_dispatched", f"order {o.order_id} is {o.task_status}")
            if o.unroutable:
                return Decision.block("unroutable", f"no online analyzer offers LOINC {o.unroutable}",
                                      "Do not substitute a test. Escalate for referral or to bring an analyzer online.")
            return Decision.ok()
        if procedure == "release_results":
            rows = self.results(args["specimen_id"])
            if not rows:
                return Decision.block("no_results", f"no results for {args['specimen_id']}")
            for r in rows:
                if r["release"] == "released":
                    return Decision.block("already_released", f"{r['result_id']} was already released")
                if r["release"] == "held":
                    return Decision.block("result_held", f"{r['result_id']} ({r['test_display']}) is held: "
                                                         f"{r['hold_reasons']}", _PHYSICAL)
                if r["order_id"] and self.orders[r["order_id"]].task_status not in ("accepted", "in-progress"):
                    o = self.orders[r["order_id"]]
                    return Decision.block("order_state", f"order {o.order_id} is {o.task_status}; results only fulfil "
                                                         "a dispatched order", "Escalate.")
                if r["release"] == "pending_ack":
                    return Decision.block("critical_unacknowledged", f"{r['result_id']} is critical "
                                          f"({r['value']} {r['unit']}) and not acknowledged",
                                          "Call acknowledge_critical; a human must approve it.")
                dev, _ = self._index[r["result_id"]]
                qc = self.qc_status(dev, r["vendor_raw"]["code"])
                if qc.status not in ("pass", "warn"):
                    return Decision.block("qc_gate", f"QC for {r['vendor_raw']['code']} on {dev} is {qc.status} "
                                                     f"{qc.violations}", "No patient release without passing QC. Escalate.")
            return Decision.ok()
        if procedure == "acknowledge_critical":
            r = self._find(args["result_id"])
            if r is None:
                return Decision.block("unknown_result", f"no result {args['result_id']}")
            if r["release"] != "pending_ack":
                return Decision.block("not_critical", f"{args['result_id']} is not awaiting acknowledgement")
            return Decision.ok()
        if procedure == "request_recollection":
            return Decision.ok()
        if procedure == "chart_vitals":
            v = self.vitals(args["device_id"])
            if "error" in v:
                return Decision.block("device_unreachable", v["error"])
            if v["charted"]:
                return Decision.block("already_charted", f"{v['set_id']} was already charted")
            if v["hold_reasons"]:
                return Decision.block("vitals_held", f"cannot chart: {v['hold_reasons']}",
                                      "Artefact and lead-off values are not measurements of the patient, and an "
                                      "unassociated monitor has no patient. Ask the nurse to check at the bedside.")
            return Decision.ok()
        return Decision.block("unknown_procedure", f"{self.app_id} has no procedure {procedure!r}")

    def _find(self, result_id: str) -> dict | None:
        if result_id not in self._index:
            return None
        _, spec = self._index[result_id]
        return next((r for r in self.results(spec) if r["result_id"] == result_id), None)

    # ------------------------------------------------------------------
    # invoke
    # ------------------------------------------------------------------

    NEEDS_HUMAN = {"acknowledge_critical", "chart_vitals"}

    def invoke(self, procedure: str, args: dict, actor: str = "agent") -> dict:
        d = validate(self.manifest(), procedure, args) or self.check(procedure, args)
        if not d.allowed:
            self.runtime.record(actor, "app", self.app_id, procedure, args, "blocked", d.rule, d.reason)
            return {"status": "blocked", "rule": d.rule, "reason": d.reason, "hint": d.hint}
        if procedure in self.NEEDS_HUMAN:
            rid = self.operator.request(self.app_id, procedure, args, actor)
            self.runtime.record(actor, "app", self.app_id, procedure, args, "needs_approval")
            return {"status": "needs_approval", "request_id": rid,
                    "reason": "a named human must approve this; poll operator approval status"}
        result = self._execute(procedure, args)
        self.runtime.record(actor, "app", self.app_id, procedure, args,
                            "executed" if result.get("status") == "ok" else "error", reason=result.get("error", ""))
        return result

    def execute_approved(self, procedure: str, args: dict, approver: str) -> dict:
        d = self.check(procedure, args)
        if not d.allowed:
            self.runtime.record(approver, "approval", self.app_id, procedure, args, "blocked", d.rule, d.reason)
            return {"status": "blocked", "rule": d.rule, "reason": d.reason}
        result = self._execute(procedure, {**args, "_approver": approver})
        self.runtime.record(approver, "approval", self.app_id, procedure, args, "executed")
        return result

    def _execute(self, procedure: str, args: dict) -> dict:
        if procedure == "dispatch_order":
            o = self.orders[args["order_id"]]
            delivered = []
            for dev, tests in o.routing.items():
                res = self.runtime.invoke(dev, "download_order", {"specimen_id": o.specimen_id, "patient_id": o.patient_id,
                                                                  "tests": tests, "order_id": o.order_id,
                                                                  "priority": o.priority}, actor=self.actor)
                if res.get("status") != "ok":
                    self._task(o, "requested", f"download to {dev} failed: {res.get('error') or res.get('rule')}")
                    return {**res, "order_id": o.order_id, "delivered": delivered}
                delivered.append({"device_id": dev, **res})
            self._task(o, "accepted")
            return {"status": "ok", "order_id": o.order_id, "delivered": delivered}
        if procedure == "release_results":
            return self._release(args["specimen_id"])
        if procedure == "acknowledge_critical":
            r = self._find(args["result_id"])
            recipient = args.get("recipient") or "responsible clinician"
            self.acks.setdefault(r["result_id"], []).append(
                {"note": args["note"], "recipient": recipient, "approved_by": args.get("_approver", "")})
            comm = fhir.communication(f"comm-{next(self._ids):04d}", patient_id=r["patient_id"],
                                      about=f"Observation/{r['result_id']}", recipient=recipient,
                                      text=f"Critical result {r['test_display']} {r['value']} {r['unit']} "
                                           f"({r['interpretation']}). {args['note']}")
            self.ehr.send(fhir.transaction([comm]))
            return {"status": "ok", "result_id": r["result_id"], "communication": comm["id"]}
        if procedure == "request_recollection":
            spec = args["specimen_id"]
            self.recollections[spec] = args["reason"]
            rows = self.results(spec)
            patient = rows[0]["patient_id"] if rows else ""
            resources = [fhir.task(f"task-recollect-{spec}", "requested", focus=spec, patient_id=patient,
                                   code_text="recollect-specimen", description=f"Recollect {spec}: {args['reason']}")]
            order = self.order_for(spec)
            if order:
                self._task(order, "on-hold", f"awaiting recollection: {args['reason']}", send=False)
                resources.append(self._task_resource(order))
            self.ehr.send(fhir.transaction(resources))
            return {"status": "ok", "specimen_id": spec, "task": resources[0]["id"]}
        if procedure == "chart_vitals":
            v = self.vitals(args["device_id"])
            results = [CanonicalResult(**{**r, "release": ReleaseState.RELEASABLE}) for r in v["results"]]
            self.ehr.send(fhir.vital_signs(results))
            self.charted.add(v["set_id"])
            for r in results:
                self.released.add(r.result_id)
            return {"status": "ok", "set_id": v["set_id"], "observations": len(results)}
        return {"status": "error", "error": f"no executor for {procedure}"}

    def _release(self, specimen_id: str) -> dict:
        rows = self.results(specimen_id)
        if not rows:
            return {"status": "error", "error": f"no results for {specimen_id}"}
        results = [CanonicalResult(**{**r, "release": ReleaseState.RELEASABLE}) for r in rows]
        order = self.order_for(specimen_id)
        report_id = f"rpt-{next(self._ids):04d}"
        bundle = fhir.diagnostic_report(report_id, results, based_on=order.order_id if order else "")
        if order:
            order.report_id = report_id
            self._task(order, "completed", send=False)
            bundle["entry"].append({"resource": self._task_resource(order),
                                    "request": {"method": "PUT", "url": f"Task/{order.task_id}"}})
        ack = self.ehr.send(bundle)
        self.released.update(r.result_id for r in results)
        return {"status": "ok", "specimen_id": specimen_id, "observations": len(results), "ehr": ack,
                "based_on": order.order_id if order else None}

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def _task_resource(self, o: Order) -> dict:
        return fhir.task(o.task_id, o.task_status, focus=f"ServiceRequest/{o.order_id}", patient_id=o.patient_id,
                         description=f"Fulfil order {o.order_id}", status_reason=o.status_reason,
                         outputs=[f"DiagnosticReport/{o.report_id}"] if o.report_id else None)

    def _task(self, o: Order, status: str, reason: str = "", send: bool = True) -> None:
        o.task_status, o.status_reason = status, reason
        o.history.append({"status": status, "reason": reason})
        if send:
            self.ehr.send(fhir.transaction([self._task_resource(o)]))
