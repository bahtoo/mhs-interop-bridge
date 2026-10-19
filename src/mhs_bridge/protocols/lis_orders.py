"""Host-to-instrument order messages for clinical analyzers.

* ASTM E1394 host order: H, P, O (action code N = new, priority S/A/R), L,
  sent over an E1381 session with the host as sender.
* HL7 v2.5.1 OML^O33: specimen-centred laboratory order (IHE LAW).

Encoders take plain values so any driver can use them; decoders are what the
simulated instrument runs on receipt, and are useful in tests.
"""

from __future__ import annotations

from . import astm, hl7

_PRIORITY = {"stat": "S", "asap": "A", "urgent": "A", "routine": "R"}


def astm_host_order(specimen_id: str, patient_id: str, tests: list[str], priority: str = "routine") -> str:
    o = ["O", "1", specimen_id, "", "^^^" + "\\^^^".join(tests), _PRIORITY.get(priority, "R"),
         "", "", "", "", "", "N"]
    return astm.build_message([["H", "", "", "HOST^LIS"], ["P", "1", patient_id], o, ["L", "1", "N"]])


def decode_astm_host_order(message: str) -> dict:
    delims, recs = astm.parse_message(message)
    p = next(r for r in recs if r.type == "P")
    o = next(r for r in recs if r.type == "O")
    if o.get(12) not in ("N", "A"):
        raise astm.ASTMError(f"unsupported action code {o.get(12)!r}")
    tests = [t.split(delims.component)[-1] for t in o.get(5).split(delims.repeat) if t]
    return {"specimen_id": o.get(3), "patient_id": p.get(3), "tests": tests, "priority": o.get(6)}


def hl7_oml_o33(specimen_id: str, patient_id: str, tests: list[str], order_id: str, control_id: str,
                priority: str = "routine") -> str:
    prio = {"stat": "S", "urgent": "A", "asap": "A"}.get(priority, "R")
    segs = [["MSH", "^~\\&", "HOST", "LAB", "ANALYZER", "LAB", hl7.now_ts(), "", "OML^O33^OML_O33",
             control_id, "P", "2.5.1"],
            ["PID", "1", "", f"{patient_id}^^^HOSP^MR"],
            ["SPM", "1", f"{specimen_id}^LAB"]]
    for i, code in enumerate(tests, start=1):
        segs.append(["ORC", "NW", f"{order_id}-{i}", "", "", "", "", f"^^^^^{prio}"])
        segs.append(["OBR", str(i), f"{order_id}-{i}", "", f"{code}^{code}^L"])
    return "\r".join("|".join(s) for s in segs) + "\r"


def decode_oml_o33(message: str) -> dict:
    msg = hl7.parse(message)
    if msg.message_type != "OML^O33":
        raise hl7.HL7Error(f"expected OML^O33, got {msg.message_type}")
    spm, pid = msg.first("SPM"), msg.first("PID")
    return {"specimen_id": spm.component(2, 1), "patient_id": pid.component(3, 1),
            "tests": [s.component(4, 1) for s in msg.all("OBR")], "control_id": msg.control_id}
