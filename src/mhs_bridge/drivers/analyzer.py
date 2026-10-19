"""MHS driver for clinical analyzers speaking ASTM E1381/E1394 or HL7 v2.

One driver serves any analyzer described by a dialect profile. It runs the real
low-level protocol (an E1381 session with frame validation, or MLLP with ACK),
decodes the high-level message, normalises the vendor dialect to canonical
results (LOINC, UCUM, canonical conditions), and exposes the analyzer to MHS
as states and procedures.

Driver interlock: a specimen whose last results carry a physical condition
(foam, clot, short sample) cannot be run again from the same cup.
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
             State("qc_history:<test>", "list", "every QC run for a test, oldest first")],
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
                       safety_checks=["a specimen with a physical condition cannot be re-run from the same cup"])],
            self.profile.tags, self.profile.data.get("risk_class", ""), driver="analyzer")

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
        raise KeyError(state)

    def precheck(self, procedure: str, args: dict):
        if procedure == "run_specimen":
            conditions = sorted({c for r in self.results.get(args["specimen_id"], []) for c in r.conditions})
            if conditions:
                return Decision.block("specimen_quarantine",
                                      f"specimen {args['specimen_id']} showed {conditions}; re-running the same cup "
                                      "is not allowed", PHYSICAL_HINT)
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
        message = self.instrument.result_message(specimen, tests, f"MSG{next(self._ctl):05d}")
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
