"""MHS driver for clinical analyzers speaking ASTM E1381/E1394 or HL7 v2.

One driver serves any analyzer described by a dialect profile. It runs the real
low-level protocol (an E1381 session with frame validation, or MLLP with ACK),
decodes the high-level message, normalises the vendor dialect to canonical
results (LOINC, UCUM, canonical conditions), and exposes the analyzer to MHS
as states and procedures.

Driver interlock: a specimen whose last results carry a physical condition
(foam, clot, short sample) cannot be run again from the same cup, unless the
analyzer's own event log shows that it caused the condition during that run
(an aspiration error) and the fault has since cleared. Then the tube was never
the problem, and re-running it is the right thing to do.

Operations: when the profile has an ``operations`` section, the analyzer also
exposes what its vendor's remote service works with: sensors with their normal
ranges, the event log, reagents on board, calibrations, the maintenance
schedule, and remote maintenance, calibration and reset. Hands-on tasks are
listed, never invocable: a person does them.

Two more interlocks: no patient specimen while the start-of-day maintenance is
due, and no more remote resets of a hardware fault than the profile's limit.
"""

from __future__ import annotations

import itertools
from typing import Any

from ..mhs_shim import Decision, Manifest, Param, Procedure, State
from ..protocols import astm, hl7, lis_orders
from ..sim.clinical_instruments import SimAnalyzer
from .dialects import Profile, normalise
from .interlocks import PHYSICAL_HINT


class AnalyzerDriver:
    def __init__(self, profile: Profile, instrument: SimAnalyzer):
        self.profile = profile
        self.device_id = profile.device_id
        self.instrument = instrument
        self.results: dict[str, list] = {}          # specimen -> canonical results, latest run
        self.qc_history: dict[str, list[dict]] = {}  # test -> every QC run, in order
        self.wire = {"sessions": 0, "aborted": 0, "retransmissions": 0, "acks": 0, "log": []}
        self._ctl = itertools.count(1)

    @property
    def online(self) -> bool:
        return self.instrument.online

    def manifest(self) -> Manifest:
        tests = ", ".join(self.profile.tests)
        interface = ("ASTM E1381/E1394 (CLSI LIS1/LIS2)" if self.profile.protocol == "astm"
                     else "HL7 v2.5.1 (IHE LAW: OML^O33 down, OUL^R22 up) over MLLP")
        return Manifest(
            self.device_id, self.profile.device_type, self.profile.data["vendor"], self.profile.data["model"],
            interface,
            [State("status", "string", "online or offline"),
             State("connection", "object", "wire statistics: sessions, aborts, retransmissions, recent log"),
             State("catalogue", "object", "tests this analyzer offers: vendor code to LOINC"),
             State("worklist", "object", "orders downloaded to the analyzer, by specimen barcode"),
             State("results:<specimen_id>", "list", "canonical results for a specimen"),
             State("qc_targets", "object", "QC material targets per test and level (mean, sd) in vendor units"),
             State("qc_history:<test>", "list", "every QC run for a test, oldest first")] + self._ops_states(),
            [Procedure("download_order", "Send an order to the analyzer's worklist in its own protocol.",
                       [Param("specimen_id", "string", "specimen barcode"), Param("patient_id", "string", "patient id"),
                        Param("tests", "list of string", f"vendor test codes, subset of {tests}"),
                        Param("order_id", "string", "order reference"),
                        Param("priority", "string", "routine, urgent or stat", required=False,
                              allowed=["routine", "urgent", "asap", "stat"])],
                       errors={"ASTM_SESSION_ABORTED": "frame NAKed six times; nothing was delivered"}),
             Procedure("run_qc", "Run a quality-control level for one test. The QC result arrives over the same "
                       "interface as patient results.",
                       [Param("test", "string", f"vendor test code, one of {tests}"),
                        Param("level", "string", "QC level, for example L1")]),
             Procedure("run_specimen", "Analyse a specimen. Results are decoded and normalised to LOINC and UCUM.",
                       [Param("specimen_id", "string", "specimen barcode"),
                        Param("tests", "list of string", f"vendor test codes, subset of {tests}")],
                       errors={"ASTM_SESSION_ABORTED": "frame NAKed six times; no results were received"},
                       safety_checks=["a specimen with a physical condition cannot be re-run from the same cup, "
                                      "unless the analyzer logged that it caused the condition and has recovered"])]
            + self._ops_procedures(),
            self.profile.tags, self.profile.data.get("risk_class", ""), driver="analyzer")

    @property
    def ops(self) -> dict:
        return self.profile.data.get("operations", {})

    def _ops_states(self) -> list[State]:
        if not self.ops:
            return []
        return [State("mode", "object", "ready, or error with the vendor code that stopped the analyzer"),
                State("sensors", "object", "sensor readings with their normal ranges"),
                State("event_log", "list", "the instrument's event log: vendor codes, newest last"),
                State("error_codes", "object", "what each vendor event code means"),
                State("reagents", "object", "reagent packs on board: lot, tests left, expiry"),
                State("calibration:<test>", "object", "the calibration in use for a test: lot, valid until"),
                State("maintenance_tasks", "object", "remote tasks (run_maintenance) and hands-on tasks (a person "
                                                     "at the instrument does them; request a work instruction)"),
                State("maintenance_schedule", "object", "scheduled maintenance: interval and whether it is due"),
                State("field_service", "object", "faults that need a field engineer, the part, and whether a "
                                                 "remote reset should be tried first")]

    def _ops_procedures(self) -> list[Procedure]:
        if not self.ops:
            return []
        tests = ", ".join(self.profile.tests)
        return [Procedure("run_maintenance", "Run a remote maintenance task on the analyzer.",
                          [Param("task", "string", "remote task", allowed=list(self.ops.get("remote_tasks", {})))]),
                Procedure("calibrate", "Calibrate a test with the calibrator on board, against the reagent lot in use.",
                          [Param("test", "string", f"vendor test code, one of {tests}")],
                          errors={"E-405": "calibration failed; see the event log"}),
                Procedure("reset_analyzer", "Remote reset of the analyzer's controller. Clears a transient fault; a "
                          "hardware fault comes back.", [],
                          safety_checks=[f"at most {self.ops.get('reset_limit', 2)} resets of a hardware fault"])]

    def perform_by_hand(self, task: str, by: str) -> dict:
        """Not a procedure: a person at the instrument did ``task``, as its own maintenance screen records it."""
        if task not in self.ops.get("hands_on_tasks", {}):
            return {"status": "error", "error": "UNKNOWN_TASK", "detail": task}
        return self.instrument.perform_by_hand(task, by)

    def read(self, state: str) -> Any:
        if state == "status":
            return "online" if self.online else "offline"
        if state == "connection":
            return {**{k: v for k, v in self.wire.items() if k != "log"}, "log": self.wire["log"][-10:]}
        if state == "catalogue":
            return {code: spec["loinc"] for code, spec in self.profile.tests.items()}
        if state == "worklist":
            return self.instrument.worklist
        if state.startswith("results:"):
            return [r.to_dict() for r in self.results.get(state.split(":", 1)[1], [])]
        if state == "qc_targets":
            return self.profile.data.get("qc", {})
        if state.startswith("qc_history:"):
            return self.qc_history.get(state.split(":", 1)[1], [])
        if self.ops:
            if state == "mode":
                return self.instrument.mode()
            if state == "maintenance_schedule":
                return self.instrument.schedule()
            if state == "field_service":
                return self.ops.get("field_service", {})
            if state == "sensors":
                return self.instrument.sensors()
            if state == "event_log":
                return self.instrument.events[-20:]
            if state == "error_codes":
                return self.ops.get("error_codes", {})
            if state == "reagents":
                return self.instrument.reagents
            if state.startswith("calibration:"):
                return self.instrument.calibration[state.split(":", 1)[1]]
            if state == "maintenance_tasks":
                return {"remote": self.ops.get("remote_tasks", {}), "hands_on": self.ops.get("hands_on_tasks", {})}
        raise KeyError(state)

    def _caused_by_instrument(self, specimen: str) -> bool:
        """The analyzer logged an aspiration error on this specimen's last run, and pressure is back to normal."""
        ids = {r.vendor_raw.get("message_id") for r in self.results.get(specimen, [])}
        logged = any(e["code"] == "E-217" and e.get("message_id") in ids for e in self.instrument.events)
        p = self.instrument.sensors().get("probe_pressure_kpa")
        recovered = p is not None and p["normal"][0] <= p["value"] <= p["normal"][1]
        return logged and recovered

    def precheck(self, procedure: str, args: dict):
        if procedure == "run_specimen":
            conditions = sorted({c for r in self.results.get(args["specimen_id"], []) for c in r.conditions})
            if conditions and not self._caused_by_instrument(args["specimen_id"]):
                return Decision.block("specimen_quarantine",
                                      f"specimen {args['specimen_id']} showed {conditions}; re-running the same cup "
                                      "is not allowed", PHYSICAL_HINT)
            if self.instrument.due:
                return Decision.block("maintenance_due", f"start-of-day maintenance is due on {self.device_id}: "
                                      f"{sorted(self.instrument.due)}",
                                      "Run the due maintenance, check the reagent packs and pass QC before patient "
                                      "specimens.")
        if procedure == "reset_analyzer" and self.instrument.hw_fault:
            limit = self.ops.get("reset_limit", 2)
            if self.instrument.resets >= limit:
                return Decision.block("reset_limit", f"{self.device_id} has been reset {self.instrument.resets} "
                                      f"times on {self.instrument.hw_fault}",
                                      "The fault is not transient. Open a service case and dispatch a field engineer "
                                      "with the part; more resets wear the analyzer and lose its logs.")
        return None

    def write(self, procedure: str, args: dict) -> dict:
        if procedure == "download_order":
            return self._download(args)
        if procedure == "run_qc":
            if args["level"] not in self.profile.data.get("qc", {}).get(args["test"], {}):
                return {"status": "error", "error": "NO_QC_MATERIAL", "detail": f"{args['test']} {args['level']}"}
            return self._run(f"QC-{args['test']}-{args['level']}", [args["test"]])
        if procedure == "run_specimen":
            return self._run(args["specimen_id"], list(args["tests"]))
        if procedure == "run_maintenance":
            return self.instrument.maintenance(args["task"])
        if procedure == "calibrate":
            if args["test"] not in self.profile.tests:
                return {"status": "error", "error": "UNKNOWN_TEST", "detail": args["test"]}
            return self.instrument.calibrate(args["test"])
        if procedure == "reset_analyzer":
            return self.instrument.reset()
        raise KeyError(procedure)

    # --- wire ------------------------------------------------------------------

    def _download(self, a: dict) -> dict:
        unknown = [t for t in a["tests"] if t not in self.profile.tests]
        if unknown:
            return {"status": "error", "error": "UNKNOWN_TEST", "detail": f"analyzer does not offer {unknown}"}
        prio = a.get("priority", "routine")
        if self.profile.protocol == "astm":
            delivered = self._astm_session(lis_orders.astm_host_order(a["specimen_id"], a["patient_id"], a["tests"], prio))
            if delivered is None:
                return {"status": "error", "error": "ASTM_SESSION_ABORTED", "detail": "order not delivered"}
            entry = self.instrument.receive_order(delivered)
        else:
            text = hl7.mllp_unwrap(hl7.mllp_wrap(lis_orders.hl7_oml_o33(
                a["specimen_id"], a["patient_id"], a["tests"], a["order_id"], f"ORD{next(self._ctl):05d}", prio)))
            entry = self.instrument.receive_order(text)
            self.wire["acks"] += 1
            self.wire["log"].append(f"OML^O33 {entry['control_id']} -> AA")
        entry["order_id"] = a["order_id"]
        return {"status": "ok", "specimen_id": entry["specimen_id"], "tests": entry["tests"],
                "message": "ASTM host order" if self.profile.protocol == "astm" else "OML^O33"}

    def _run(self, specimen: str, tests: list[str]) -> dict:
        unknown = [t for t in tests if t not in self.profile.tests]
        if unknown:
            return {"status": "error", "error": "UNKNOWN_TEST", "detail": f"analyzer does not offer {unknown}"}
        refused = self.instrument.begin_run(specimen, tests)
        if refused:
            return refused
        message_id = f"MSG{next(self._ctl):05d}"
        message = self.instrument.result_message(specimen, tests, message_id)
        if self.profile.protocol == "astm":
            message = self._astm_session(message)
            if message is None:
                return {"status": "error", "error": "ASTM_SESSION_ABORTED",
                        "detail": "frame NAKed six times; E1381 session aborted, no results received",
                        "hint": "Transport failure. Do not assume results exist. Check the line and escalate."}
        else:
            msg = hl7.parse(hl7.mllp_unwrap(hl7.mllp_wrap(message)))
            self.wire["acks"] += 1
            self.wire["log"].append(f"{msg.message_type} {msg.control_id} -> {hl7.build_ack(msg).first('MSA').get(1)}")
        results = normalise(self.profile, message)
        for r in results:
            r.result_id = f"{self.device_id}-{r.result_id}"
            r.vendor_raw["message_id"] = message_id
            order = self.instrument.worklist.get(specimen)
            r.order_id = order.get("order_id", "") if order else ""
        if specimen.startswith("QC-"):
            _, test, level = specimen.split("-")
            for r in results:
                self.qc_history.setdefault(test, []).append(
                    {"level": level, "value": float(r.vendor_raw["value"]), "run_at": r.observed_at})
        else:
            self.results[specimen] = results
        return {"status": "ok", "specimen_id": specimen, "results_received": len(results)}

    def _astm_session(self, message: str) -> str | None:
        """Run an E1381 session against a receiver that validates every frame."""
        corrupt_first = int(self.instrument.faults.get("line_noise_naks", 0))
        storm = bool(self.instrument.faults.get("nak_storm", False))
        attempts: dict[bytes, int] = {}
        expected = [1]

        def channel(data: bytes) -> bytes:
            if data in (astm.ENQ, astm.EOT):
                return astm.ACK
            n = attempts.get(data, 0)
            attempts[data] = n + 1
            wire = data[:3] + bytes([data[3] ^ 0x01]) + data[4:] if (storm or n < corrupt_first) else data
            try:
                astm.parse_frame(wire, expected_fn=expected[0])
            except astm.ASTMError:
                return astm.NAK
            expected[0] = (expected[0] + 1) % 8
            return astm.ACK

        self.wire["sessions"] += 1
        res = astm.transfer(message, channel)
        self.wire["retransmissions"] += res.retransmissions
        self.wire["log"].extend(res.log)
        if res.aborted:
            self.wire["aborted"] += 1
        return res.message
