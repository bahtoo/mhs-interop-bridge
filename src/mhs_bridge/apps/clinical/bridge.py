"""Clinical bridge: an application that connects MHS devices to an EHR over FHIR.

It sits outside the MHS boundary and uses MHS the way any client does: it
discovers analyzers, reads their catalogues and results, and invokes
``download_order`` on them. On top of that it owns the clinical semantics that
are not a hardware concern:

* FHIR ServiceRequest intake and capability routing, and rerouting to a backup
  analyzer when the one an order was routed to stops;
* the order lifecycle, mirrored to the EHR as FHIR Task;
* release gates: holds, patient identity against the order, order state,
  Westgard QC, critical-value notification;
* critical-result notification (FHIR Communication), specimen recollection (Task);
* charting of nurse-validated vital signs;
* FHIR egress (Observation, DiagnosticReport, Task, Communication, Provenance).

Nothing this bridge sends leaves without a named human's approval. Every
outbound procedure is drafted instead of executed: the gates run, the real
payload is rendered (the ASTM host order or OML^O33 for each analyzer, the FHIR
Bundle for the EHR), and the draft waits in the operator channel's review queue.
On approval the gates run again and the payload is rendered again; if it no
longer matches the approved hash, nothing is sent. What is sent carries the
approver's Provenance. The agent asks; the human decides; the record says who.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

from ...canonical import CanonicalResult, ReleaseState
from ...mhs_shim import Decision, Manifest, Param, Procedure, State
from ...protocols import fhir, lis_orders
from ..base import validate
from ..operator import content_hash
from . import orders as order_intake
from .ehr import MemoryEhr
from .models import Order
from .qc import QCStatus, westgard, z_scores

_PHYSICAL = ("Held for a physical or identity reason. Do not re-run the same specimen; request a new one and "
             "escalate.")
_RESOLVABLE = {"specimen_id"}     # order fields a human may set through propose_resolution


class ClinicalBridgeApp:
    app_id = "clinical"

    # Every procedure is reviewed. The value is the draft kind.
    REVIEWED = {"dispatch_order": "lis_order", "reroute_order": "lis_reroute", "release_results": "ehr_release",
                "request_recollection": "ward_task", "request_critical_callback": "critical_callback",
                "chart_vitals": "vitals_chart", "propose_resolution": "data_resolution"}
    NEEDS_HUMAN = set(REVIEWED)

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
            State("orders", "list", "EHR orders with routing, unroutable codes, data exceptions and Task status"),
            State("order:<order_id>", "object", "one order and its lifecycle history"),
            State("results:<specimen_id>", "list", "canonical results with release state, hold reasons and exceptions"),
            State("qc_status:<device_id>:<test>", "object", "Westgard status for a test on an analyzer"),
            State("vitals:<device_id>", "object", "the monitor's latest set, whether it can be charted, and why not"),
            State("ehr_outbox", "list", "FHIR bundles sent to the EHR (in-memory EHR only)")], [
            Procedure("dispatch_order", "Draft the download of an EHR order to the analyzers it was routed to, in "
                      "each analyzer's protocol. Sent when a human approves; the Task then moves to accepted.",
                      [Param("order_id", "string", "ServiceRequest id")],
                      safety_checks=["order not yet dispatched", "every test routable to an online analyzer",
                                     "no routed analyzer stopped on a fault",
                                     "no unresolved data exception (for example a missing accession)"]),
            Procedure("reroute_order", "Draft moving an order that is not yet dispatched off a stopped analyzer, to "
                      "analyzers in service that offer its tests. Applied when a human approves.",
                      [Param("order_id", "string", "ServiceRequest id"),
                       Param("reason", "string", "why, with the analyzer's state")],
                      safety_checks=["order not yet dispatched", "an analyzer it is routed to is stopped",
                                     "every test routable to an analyzer in service"]),
            Procedure("release_results", "Draft a specimen's results for the EHR as Observations and a "
                      "DiagnosticReport. Sent when a human approves.",
                      [Param("specimen_id", "string", "specimen barcode")],
                      safety_checks=["no held results", "patient matches the order", "order dispatched",
                                     "latest QC passes Westgard", "critical values telephoned by a human"]),
            Procedure("request_critical_callback", "Ask a human to telephone a critical value to the responsible "
                      "clinician. The human makes the call. Approving the request records that they did, under their "
                      "name, and sends a FHIR Communication; rejecting it means the call was not made and the result "
                      "stays held.",
                      [Param("result_id", "string", "result id"),
                       Param("recipient", "string", "who should be called", required=False),
                       Param("context", "string", "anything the caller should know", required=False)]),
            Procedure("request_recollection", "Draft a request to the ward for a new specimen (FHIR Task). When a "
                      "human approves, it is sent and the order goes on hold.",
                      [Param("specimen_id", "string", "specimen barcode"), Param("reason", "string", "why")]),
            Procedure("chart_vitals", "Chart a bedside monitor's latest set as vital-signs Observations. "
                      "Needs a nurse's validation through the operator channel.",
                      [Param("device_id", "string", "patient monitor")],
                      safety_checks=["monitor associated with a patient", "no artefact or lead-off values",
                                     "set not already charted"]),
            Procedure("propose_resolution", "Propose a value for a field an order's data exception says is missing, "
                      "for a human to confirm, edit or reject. Nothing changes until a human approves. Cite the basis.",
                      [Param("order_id", "string", "ServiceRequest id"),
                       Param("exception_id", "string", "the exception being resolved"),
                       Param("value", "string", "the proposed value"),
                       Param("basis", "string", "where the value comes from")],
                      safety_checks=["exception exists and is unresolved", "field may be set by a human"])],
            ["Held results stay held. Do not look for a way around a gate.",
             "A patient mismatch between order and result is a potential wrong-patient specimen.",
             "Every procedure is drafted for a named human's review; nothing is sent until they approve it.",
             "Suggestions attached to a data exception are unverified. Never present one as a fact."])

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
        notes = [f"unroutable: {', '.join(order.unroutable)}"] if order.unroutable else []
        notes += [f"needs review: {e['issue']}" for e in order.exceptions]
        self._task(order, "requested", "; ".join(notes))
        self.runtime.record("ehr", "ehr", self.app_id, "receive_order", {"id": order.order_id}, "ok")
        return {"status": "ok", "order_id": order.order_id, "routing": order.routing, "unroutable": order.unroutable,
                "exceptions": [e["exception_id"] for e in order.exceptions]}

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
            down = [dev for dev in o.routing if not order_intake.in_service(self.runtime, dev, self.actor)]
            if down:
                return Decision.block("analyzer_down", f"order {o.order_id} is routed to {down}, stopped on a fault",
                                      "Move the order to an analyzer in service with reroute_order, and tell the "
                                      "vendor's service desk about the stopped one.")
            if o.exceptions:
                return Decision.block("data_exception", f"order {o.order_id} has unresolved data exceptions: "
                                      f"{[e['issue'] for e in o.exceptions]}",
                                      "A human must confirm the missing value. Read the order's exceptions and their "
                                      "suggestions, then propose_resolution with the basis, or escalate.")
            return Decision.ok()
        if procedure == "reroute_order":
            o = self.orders.get(args["order_id"])
            if o is None:
                return Decision.block("unknown_order", f"no order {args['order_id']}")
            if o.task_status != "requested":
                return Decision.block("already_dispatched", f"order {o.order_id} is {o.task_status}")
            if all(order_intake.in_service(self.runtime, dev, self.actor) for dev in o.routing):
                return Decision.block("not_needed", f"every analyzer order {o.order_id} is routed to is in service")
            if self._rerouted(o).unroutable:
                return Decision.block("unroutable", f"no analyzer in service offers LOINC {self._rerouted(o).unroutable}",
                                      "Escalate: refer the specimen to another lab, or wait for the analyzer.")
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
                                          "Call request_critical_callback; a human must make the call.")
                dev, _ = self._index[r["result_id"]]
                if self._instrument_suspect(args["specimen_id"]):
                    return Decision.block("instrument_fault", f"{dev} logged an instrument error during the run of "
                                          f"{args['specimen_id']}", "Results from a run during an instrument fault "
                                          "are not valid. Fix the analyzer, then re-run the specimen.")
                qc = self.qc_status(dev, r["vendor_raw"]["code"])
                if qc.status not in ("pass", "warn"):
                    return Decision.block("qc_gate", f"QC for {r['vendor_raw']['code']} on {dev} is {qc.status} "
                                                     f"{qc.violations}", "No patient release without passing QC. Escalate.")
            return Decision.ok()
        if procedure == "request_critical_callback":
            r = self._find(args["result_id"])
            if r is None:
                return Decision.block("unknown_result", f"no result {args['result_id']}")
            if r["release"] != "pending_ack":
                return Decision.block("not_critical", f"{args['result_id']} is not awaiting a critical callback")
            return Decision.ok()
        if procedure == "request_recollection":
            suspect = self._instrument_suspect(args["specimen_id"])
            if suspect:
                return Decision.block("instrument_suspected", f"{suspect} logged an instrument error during the run "
                                      f"of {args['specimen_id']}; the flag may not be the specimen's fault",
                                      "Investigate the analyzer first (event log, sensors). Calling a patient back "
                                      "for an instrument fault is harm without benefit.")
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
        if procedure == "propose_resolution":
            o = self.orders.get(args["order_id"])
            if o is None:
                return Decision.block("unknown_order", f"no order {args['order_id']}")
            exc = next((e for e in o.exceptions if e["exception_id"] == args["exception_id"]), None)
            if exc is None:
                return Decision.block("unknown_exception", f"order {o.order_id} has no open exception "
                                                           f"{args['exception_id']!r}")
            if exc["field"] not in _RESOLVABLE:
                return Decision.block("not_resolvable", f"{exc['field']} cannot be set through the bridge")
            if not str(args["value"]).strip():
                return Decision.block("missing_value", "propose a value; to say there is none, escalate instead")
            return Decision.ok()
        return Decision.block("unknown_procedure", f"{self.app_id} has no procedure {procedure!r}")

    def _instrument_suspect(self, specimen_id: str) -> str:
        """The analyzer whose event log has an error tied to the run that produced this specimen's results."""
        for r in self.results(specimen_id):
            dev = r["device_id"]
            log = self.runtime.read(dev, "event_log", actor=self.actor)
            if isinstance(log, list) and any(e.get("message_id") == r["vendor_raw"].get("message_id")
                                             and e.get("code", "").startswith("E-") for e in log):
                return dev
        return ""

    def _find(self, result_id: str) -> dict | None:
        if result_id not in self._index:
            return None
        _, spec = self._index[result_id]
        return next((r for r in self.results(spec) if r["result_id"] == result_id), None)

    # ------------------------------------------------------------------
    # invoke: gate, then draft for review
    # ------------------------------------------------------------------

    def invoke(self, procedure: str, args: dict, actor: str = "agent", rationale: str = "") -> dict:
        d, draft = self.prepare(procedure, args, actor, rationale)
        if not d.allowed and d.rule == "cannot_draft":      # nothing to send, not a safety rule
            self.runtime.record(actor, "app", self.app_id, procedure, args, "error", reason=d.reason)
            return {"status": "error", "error": "cannot_draft", "detail": d.reason}
        if not d.allowed:
            self.runtime.record(actor, "app", self.app_id, procedure, args, "blocked", d.rule, d.reason)
            return {"status": "blocked", "rule": d.rule, "reason": d.reason, "hint": d.hint}
        rid = self.operator.request(self.app_id, procedure, args, actor, draft)
        req = self.operator.approvals[rid]
        self.runtime.record(actor, "app", self.app_id, procedure, args, "needs_approval", reason=rid)
        return {"status": "needs_approval", "request_id": rid, "kind": req.kind,
                "payload_sha256": req.payload_sha256, "exceptions": req.exceptions,
                "reason": "drafted for review (PENDING_REVIEW): a named human approves, rejects or revises it; "
                          "poll operator_approval_status"}

    def prepare(self, procedure: str, args: dict, actor: str = "agent", rationale: str = "",
                prior: Any = None) -> tuple[Decision, dict]:
        """Run the gates and, if they pass, render the draft. Used for a new draft and for a human's revision."""
        d = validate(self.manifest(), procedure, args) or self.check(procedure, args)
        if not d.allowed:
            return d, {}
        try:
            return d, self._draft(procedure, args, rationale, prior)
        except (KeyError, ValueError, TypeError) as exc:
            return Decision.block("cannot_draft", f"{procedure} could not be rendered: {type(exc).__name__}: {exc}"), {}

    def execute_approved(self, procedure: str, args: dict, approver: str, draft: Any) -> dict:
        event = self.operator.authorise(draft)
        d = self.check(procedure, args)
        if not d.allowed:
            self.runtime.record(approver, "approval", self.app_id, procedure, args, "blocked", d.rule, d.reason)
            return {"status": "blocked", "rule": d.rule, "reason": d.reason}
        fresh = self._draft(procedure, args, draft.intent.get("agent_rationale", ""), draft)
        now = content_hash(fresh["payload"]["content"])
        if now != draft.payload_sha256:
            reason = (f"what would be sent now (sha256 {now[:12]}) differs from what was approved "
                      f"({draft.payload_sha256[:12]}); nothing was sent. Draft it again for review.")
            self.runtime.record(approver, "approval", self.app_id, procedure, args, "blocked", "payload_changed", reason)
            return {"status": "blocked", "rule": "payload_changed", "reason": reason}
        result = self._execute(procedure, args, approver, draft, event, fresh)
        self.runtime.record(approver, "approval", self.app_id, procedure, args,
                            "executed" if result.get("status") == "ok" else "error", reason=result.get("error", ""))
        return result

    # ------------------------------------------------------------------
    # drafts: render without sending
    # ------------------------------------------------------------------

    def _draft(self, procedure: str, args: dict, rationale: str, prior: Any) -> dict:
        meta = dict(prior.payload.get("meta", {})) if prior is not None else {}
        kind = self.REVIEWED[procedure]
        safety = self.manifest().procedure(procedure).safety_checks
        if procedure == "dispatch_order":
            out = self._draft_dispatch(self.orders[args["order_id"]])
        elif procedure == "reroute_order":
            out = self._draft_reroute(self.orders[args["order_id"]], args["reason"])
        elif procedure == "release_results":
            meta.setdefault("report_id", f"rpt-{next(self._ids):04d}")
            out = self._draft_release(args["specimen_id"], meta["report_id"])
        elif procedure == "request_recollection":
            out = self._draft_recollection(args["specimen_id"], args["reason"])
        elif procedure == "request_critical_callback":
            out = self._draft_callback(args)
        elif procedure == "chart_vitals":
            out = self._draft_vitals(args["device_id"])
        else:
            out = self._draft_resolution(args)
        out["intent"] = {**out["intent"], "agent_rationale": rationale}
        out["payload"]["meta"] = {**out["payload"].get("meta", {}), **meta}
        return {"kind": kind, "gates": {"allowed": True, "checked": safety}, "exceptions": [], **out}

    def _source(self, order: Order | None, specimen_id: str = "") -> dict:
        if order is None:
            return {"type": "paper_request", "id": specimen_id, "raw": {},
                    "text": "No EHR order: paper request form. Released against the patient on the specimen label."}
        sr = order.source
        note = " ".join(n.get("text", "") for n in sr.get("note", []))
        text = (f"{sr.get('priority', 'routine')} ServiceRequest {order.order_id} for patient {order.patient_id}, "
                f"specimen {order.specimen_id or 'MISSING'}: LOINC {', '.join(order.loinc_codes)}"
                + (f". Note: {note}" if note else ""))
        return {"type": "ServiceRequest", "id": order.order_id, "text": text, "raw": sr}

    def _draft_dispatch(self, o: Order) -> dict:
        resolved = next((h for h in reversed(o.history) if h.get("resolved") == "specimen_id"), None)
        spec_from = (f"resolution {resolved['request_id']} approved by {resolved['by']}" if resolved
                     else "intent.source.raw.specimen[0].identifier.value")
        downloads, texts, proposed = [], [], []
        for dev, tests in o.routing.items():
            a = {"specimen_id": o.specimen_id, "patient_id": o.patient_id, "tests": tests, "order_id": o.order_id,
                 "priority": o.priority}
            downloads.append({"device_id": dev, "args": a})
            protocol = self.runtime.drivers[dev].profile.protocol
            if protocol == "astm":
                msg = lis_orders.astm_host_order(o.specimen_id, o.patient_id, tests, o.priority)
            else:
                msg = lis_orders.hl7_oml_o33(o.specimen_id, o.patient_id, tests, o.order_id, "<set at send>",
                                             o.priority)
            texts.append(f"-> {dev} ({'ASTM E1394 host order' if protocol == 'astm' else 'HL7 v2 OML^O33'})\n"
                         + msg.replace("\r", "\n").strip())
            catalogue = self.runtime.read(dev, "catalogue", actor=self.actor)
            proposed += [{"path": f"{dev}.specimen_id", "value": o.specimen_id, "from": spec_from},
                         {"path": f"{dev}.patient_id", "value": o.patient_id,
                          "from": "intent.source.raw.subject.identifier.value"},
                         {"path": f"{dev}.priority", "value": o.priority, "from": "intent.source.raw.priority"}]
            proposed += [{"path": f"{dev}.tests[{i}]", "value": t, "from": f"catalogue {dev}: {t} = LOINC "
                          f"{catalogue.get(t, '?') if isinstance(catalogue, dict) else '?'}"}
                         for i, t in enumerate(tests)]
        return {"intent": {"source": self._source(o)}, "proposed": proposed,
                "payload": {"format": "lis_order", "text": "\n\n".join(texts), "content": {"downloads": downloads}}}

    def _rerouted(self, o: Order) -> Order:
        trial = Order(o.order_id, o.patient_id, o.specimen_id, list(o.loinc_codes), o.priority, o.task_id,
                      source=o.source)
        return order_intake.route(trial, self.runtime, self.actor)

    def _draft_reroute(self, o: Order, reason: str) -> dict:
        new = self._rerouted(o)
        content = {"order_id": o.order_id, "from": o.routing, "to": new.routing}
        proposed = [{"path": f"{o.order_id}.routing.{dev}", "value": tests,
                     "from": f"catalogue {dev}; {dev} is in service"} for dev, tests in new.routing.items()]
        return {"intent": {"source": {**self._source(o), "text": reason}}, "proposed": proposed,
                "payload": {"format": "lis_reroute", "content": content,
                            "text": f"Order {o.order_id}: {o.routing} -> {new.routing}\nReason: {reason}"}}

    def _draft_release(self, specimen_id: str, report_id: str) -> dict:
        rows = self.results(specimen_id)
        bundle, _, order, _ = self._render_release(specimen_id, rows, report_id)
        proposed = []
        for r in rows:
            raw = r["vendor_raw"]
            proposed += [{"path": f"Observation/{r['result_id']}.valueQuantity", "value": f"{r['value']} {r['unit']}",
                          "from": f"{r['device_id']} {raw.get('code')} raw {raw.get('value')} {raw.get('unit')}"},
                         {"path": f"Observation/{r['result_id']}.interpretation", "value": r["interpretation"],
                          "from": f"profile ranges {r['ref_low']} to {r['ref_high']} {r['unit']}"}]
        if rows:
            proposed.append({"path": "DiagnosticReport.subject", "value": rows[0]["patient_id"],
                             "from": "analyzer result record (patient on the specimen label)"})
        if order:
            proposed.append({"path": "DiagnosticReport.basedOn", "value": f"ServiceRequest/{order.order_id}",
                             "from": "intent.source.id"})
        return {"intent": {"source": self._source(order, specimen_id)}, "proposed": proposed,
                "exceptions": [e for r in rows for e in r.get("exceptions", [])],
                "payload": {"format": "fhir+json", "text": json.dumps(bundle, indent=1),
                            "content": fhir.clinical_content(bundle)}}

    def _render_release(self, specimen_id: str, rows: list[dict], report_id: str
                        ) -> tuple[dict, list[CanonicalResult], Order | None, list[str]]:
        results = [CanonicalResult(**{**r, "release": ReleaseState.RELEASABLE}) for r in rows]
        order = self.order_for(specimen_id)
        outstanding = sorted(set(order.loinc_codes) - {r.test_code for r in results}) if order else []
        bundle = fhir.diagnostic_report(report_id, results, based_on=order.order_id if order else "",
                                        partial=bool(outstanding))
        if order:
            # An order is complete only when every ordered test is reported. Reachable only when the routing gate
            # is bypassed (the guardrails-off ablation), since dispatch refuses a partly unroutable order.
            status, reason = (("in-progress", f"partial report; outstanding LOINC {', '.join(outstanding)}")
                              if outstanding else ("completed", ""))
            bundle["entry"].append({"resource": self._task_resource(order, status, reason, report_id),
                                    "request": {"method": "PUT", "url": f"Task/{order.task_id}"}})
        return bundle, results, order, outstanding

    def _recollection_bundle(self, specimen_id: str, reason: str) -> tuple[dict, Order | None]:
        rows = self.results(specimen_id)
        patient = rows[0]["patient_id"] if rows else ""
        resources = [fhir.task(f"task-recollect-{specimen_id}", "requested", focus=specimen_id, patient_id=patient,
                               code_text="recollect-specimen", description=f"Recollect {specimen_id}: {reason}")]
        order = self.order_for(specimen_id)
        if order:
            resources.append(self._task_resource(order, "on-hold", f"awaiting recollection: {reason}"))
        return fhir.transaction(resources), order

    def _draft_recollection(self, specimen_id: str, reason: str) -> dict:
        bundle, order = self._recollection_bundle(specimen_id, reason)
        return {"intent": {"source": self._source(order, specimen_id)},
                "proposed": [{"path": f"Task/task-recollect-{specimen_id}.description",
                              "value": f"Recollect {specimen_id}: {reason}", "from": "args.reason"}],
                "payload": {"format": "fhir+json", "text": json.dumps(bundle, indent=1),
                            "content": fhir.clinical_content(bundle)}}

    def _draft_callback(self, args: dict) -> dict:
        r = self._find(args["result_id"])
        recipient = args.get("recipient") or "responsible clinician"
        content = {"result_id": r["result_id"], "test": r["test_display"], "value": r["value"], "unit": r["unit"],
                   "interpretation": r["interpretation"], "patient_id": r["patient_id"], "recipient": recipient}
        text = (f"Telephone {recipient}: {r['test_display']} {r['value']} {r['unit']} ({r['interpretation']}) for "
                f"patient {r['patient_id']}, specimen {r['specimen_id']}."
                + (f" Context: {args['context']}" if args.get("context") else ""))
        return {"intent": {"source": self._source(self.order_for(r["specimen_id"]), r["specimen_id"])},
                "proposed": [{"path": "Communication.about", "value": f"Observation/{r['result_id']}",
                              "from": "args.result_id"},
                             {"path": "Communication.recipient", "value": recipient, "from": "args.recipient"},
                             {"path": "value", "value": f"{r['value']} {r['unit']}",
                              "from": f"{r['device_id']} result {r['result_id']}"}],
                "payload": {"format": "request", "text": text, "content": content}}

    def _vitals_bundle(self, device_id: str) -> tuple[dict, list[CanonicalResult], dict]:
        v = self.vitals(device_id)
        results = [CanonicalResult(**{**r, "release": ReleaseState.RELEASABLE}) for r in v["results"]]
        return fhir.vital_signs(results), results, v

    def _draft_vitals(self, device_id: str) -> dict:
        bundle, results, v = self._vitals_bundle(device_id)
        source = {"type": "monitor_set", "id": v["set_id"], "raw": v.get("association", {}),
                  "text": f"Latest set {v['set_id']} from {device_id} for patient {results[0].patient_id}"}
        return {"intent": {"source": source},
                "proposed": [{"path": f"Observation/{r.result_id}.valueQuantity", "value": f"{r.value} {r.unit}",
                              "from": f"{device_id} set {v['set_id']} {r.vendor_raw.get('code', r.test_code)}"}
                             for r in results],
                "payload": {"format": "fhir+json", "text": json.dumps(bundle, indent=1),
                            "content": fhir.clinical_content(bundle), "meta": {"set_id": v["set_id"]}}}

    def _draft_resolution(self, args: dict) -> dict:
        o = self.orders[args["order_id"]]
        exc = next(e for e in o.exceptions if e["exception_id"] == args["exception_id"])
        content = {"order_id": o.order_id, "exception_id": exc["exception_id"], "field": exc["field"],
                   "value": args["value"]}
        return {"intent": {"source": {"type": "DataException", "id": exc["exception_id"], "text": exc["detail"],
                                      "raw": exc}},
                "proposed": [{"path": f"Order/{o.order_id}.{exc['field']}", "value": args["value"], "from": "args.value"}],
                "exceptions": [exc],
                "payload": {"format": "data_resolution", "content": content,
                            "text": f"Set {exc['field']} of order {o.order_id} to {args['value']!r}. "
                                    f"Proposed basis: {args['basis']}. Check it against the specimen label."}}

    # ------------------------------------------------------------------
    # execute: only from an approved draft, through decide()
    # ------------------------------------------------------------------

    def _send(self, bundle: dict, draft: Any, approver: str, event: Any) -> dict:
        """Send to the EHR with the approval attached: a review tag on the Bundle and the approver's Provenance."""
        targets = [f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in bundle["entry"]]
        bundle["meta"] = {"tag": [{"system": fhir.REVIEW_TAG, "code": draft.request_id}]}
        prov = fhir.provenance(f"prov-{draft.request_id}", targets, approver=approver, at=event.at,
                               request_id=draft.request_id, payload_sha256=draft.payload_sha256,
                               activity=f"{draft.kind} approved")
        bundle["entry"].append({"resource": prov, "request": {"method": "PUT", "url": f"Provenance/{prov['id']}"}})
        return self.ehr.send(bundle)

    def _execute(self, procedure: str, args: dict, approver: str, draft: Any, event: Any, fresh: dict) -> dict:
        if procedure == "dispatch_order":
            o = self.orders[args["order_id"]]
            delivered = []
            for dl in draft.payload["content"]["downloads"]:      # exactly what was approved
                res = self.runtime.invoke(dl["device_id"], "download_order", dl["args"], actor=self.actor)
                if res.get("status") != "ok":
                    self._task(o, "requested", f"download to {dl['device_id']} failed: "
                                               f"{res.get('error') or res.get('rule')}")
                    return {**res, "order_id": o.order_id, "delivered": delivered}
                delivered.append({"device_id": dl["device_id"], **res})
            self._task(o, "accepted", f"dispatched on approval by {approver} ({draft.request_id})")
            return {"status": "ok", "order_id": o.order_id, "delivered": delivered}
        if procedure == "reroute_order":
            o = self.orders[args["order_id"]]
            moved = draft.payload["content"]["to"]                  # exactly what was approved
            o.routing = {dev: list(tests) for dev, tests in moved.items()}
            o.history.append({"status": o.task_status, "rerouted": moved, "by": approver,
                              "request_id": draft.request_id, "reason": args["reason"]})
            return {"status": "ok", "order_id": o.order_id, "routing": o.routing}
        if procedure == "release_results":
            return self._release(args["specimen_id"], draft, approver, event)
        if procedure == "request_critical_callback":
            r = self._find(args["result_id"])
            recipient = args.get("recipient") or "responsible clinician"
            self.acks.setdefault(r["result_id"], []).append({"recipient": recipient, "called_by": approver})
            comm = fhir.communication(f"comm-{next(self._ids):04d}", patient_id=r["patient_id"],
                                      about=f"Observation/{r['result_id']}", recipient=recipient,
                                      text=f"Critical result {r['test_display']} {r['value']} {r['unit']} "
                                           f"({r['interpretation']}) telephoned to {recipient} by {approver}.")
            self._send(fhir.transaction([comm]), draft, approver, event)
            return {"status": "ok", "result_id": r["result_id"], "communication": comm["id"]}
        if procedure == "request_recollection":
            spec = args["specimen_id"]
            bundle, order = self._recollection_bundle(spec, args["reason"])
            self.recollections[spec] = args["reason"]
            if order:
                self._task(order, "on-hold", f"awaiting recollection: {args['reason']}", send=False)
            self._send(bundle, draft, approver, event)
            return {"status": "ok", "specimen_id": spec, "task": bundle["entry"][0]["resource"]["id"]}
        if procedure == "chart_vitals":
            bundle, results, v = self._vitals_bundle(args["device_id"])
            self._send(bundle, draft, approver, event)
            self.charted.add(v["set_id"])
            for r in results:
                self.released.add(r.result_id)
            return {"status": "ok", "set_id": v["set_id"], "observations": len(results)}
        if procedure == "propose_resolution":
            o = self.orders[args["order_id"]]
            exc = next(e for e in o.exceptions if e["exception_id"] == args["exception_id"])
            setattr(o, exc["field"], str(args["value"]))
            o.exceptions.remove(exc)
            origin = (f"proposed by {draft.requested_by}: {args['basis']}" if draft.revision == 1 else
                      f"revised by a human from {draft.requested_by}'s proposal ({args['basis']})")
            o.history.append({"status": o.task_status, "resolved": exc["field"], "by": approver,
                              "request_id": draft.request_id,
                              "reason": f"{exc['field']} set to {args['value']!r} by {approver}; {origin}"})
            return {"status": "ok", "order_id": o.order_id, exc["field"]: str(args["value"])}
        return {"status": "error", "error": f"no executor for {procedure}"}

    def _release(self, specimen_id: str, draft: Any, approver: str, event: Any) -> dict:
        rows = self.results(specimen_id)
        if not rows:
            return {"status": "error", "error": f"no results for {specimen_id}"}
        report_id = draft.payload["meta"]["report_id"]
        bundle, results, order, outstanding = self._render_release(specimen_id, rows, report_id)
        if order:
            order.report_id = report_id
            if outstanding:
                self._task(order, "in-progress", f"partial report; outstanding LOINC {', '.join(outstanding)}",
                           send=False)
            else:
                self._task(order, "completed", send=False)
        ack = self._send(bundle, draft, approver, event)
        self.released.update(r.result_id for r in results)
        return {"status": "ok", "specimen_id": specimen_id, "observations": len(results), "ehr": ack,
                "based_on": order.order_id if order else None, "outstanding_tests": outstanding,
                "report": report_id}

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def _task_resource(self, o: Order, status: str | None = None, reason: str | None = None,
                       report_id: str | None = None) -> dict:
        """The order's Task, as it is or as it would be after a change (for a draft, without changing it)."""
        report = o.report_id if report_id is None else report_id
        return fhir.task(o.task_id, o.task_status if status is None else status, focus=f"ServiceRequest/{o.order_id}",
                         patient_id=o.patient_id, description=f"Fulfil order {o.order_id}",
                         status_reason=o.status_reason if reason is None else reason,
                         outputs=[f"DiagnosticReport/{report}"] if report else None)

    def _task(self, o: Order, status: str, reason: str = "", send: bool = True) -> None:
        o.task_status, o.status_reason = status, reason
        o.history.append({"status": status, "reason": reason})
        if send:
            self.ehr.send(fhir.transaction([self._task_resource(o)]))
