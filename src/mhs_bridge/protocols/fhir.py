"""FHIR R4 egress: canonical results to Observation and DiagnosticReport.

This is the EHR-facing edge of the bus. Plain dicts are produced so there is no
dependency on a FHIR library; the structure follows the R4 base resources and
uses LOINC for codes and UCUM for units.
"""

from __future__ import annotations

from ..canonical import CanonicalResult

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
INTERP = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

_INTERP_DISPLAY = {
    "N": "Normal", "L": "Low", "H": "High",
    "LL": "Critical low", "HH": "Critical high",
}


def fhir_datetime(ts: str) -> str:
    """Convert an HL7/ASTM timestamp (YYYYMMDD[HHMM[SS]]) to a FHIR dateTime.

    Device timestamps rarely carry a zone; this bridge treats them as UTC and
    says so explicitly with +00:00 rather than emitting an ambiguous local time.
    """
    ts = ts.strip()
    if len(ts) >= 14 and ts[:14].isdigit():
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}+00:00"
    if len(ts) >= 12 and ts[:12].isdigit():
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:00+00:00"
    if len(ts) >= 8 and ts[:8].isdigit():
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
    return ts


def observation(r: CanonicalResult) -> dict:
    category = "vital-signs" if r.kind == "vital" else "laboratory"
    obs: dict = {
        "resourceType": "Observation",
        "id": r.result_id,
        "status": r.status,
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": category}]}],
        "code": {"coding": [{"system": LOINC, "code": r.test_code, "display": r.test_display}]},
        "device": {"identifier": {"value": r.device_id}},
        "effectiveDateTime": fhir_datetime(r.observed_at),
    }
    if r.kind == "lab":
        obs["specimen"] = {"identifier": {"value": r.specimen_id}}
    if r.order_id:
        obs["basedOn"] = [{"reference": f"ServiceRequest/{r.order_id}"}]
    if r.patient_id:
        obs["subject"] = {"identifier": {"value": r.patient_id}}
    if r.value is not None:
        obs["valueQuantity"] = {"value": r.value, "unit": r.unit, "system": UCUM, "code": r.unit}
    if r.ref_low is not None or r.ref_high is not None:
        rng: dict = {}
        if r.ref_low is not None:
            rng["low"] = {"value": r.ref_low, "unit": r.unit, "system": UCUM, "code": r.unit}
        if r.ref_high is not None:
            rng["high"] = {"value": r.ref_high, "unit": r.unit, "system": UCUM, "code": r.unit}
        obs["referenceRange"] = [rng]
    if r.interpretation:
        obs["interpretation"] = [{"coding": [{
            "system": INTERP, "code": r.interpretation,
            "display": _INTERP_DISPLAY.get(r.interpretation, r.interpretation)}]}]
    if r.conditions:
        obs["note"] = [{"text": "Instrument conditions: " + ", ".join(r.conditions)}]
    return obs


def diagnostic_report(report_id: str, results: list[CanonicalResult], based_on: str = "",
                      partial: bool = False) -> dict:
    """A transaction Bundle holding one DiagnosticReport and its Observations.

    ``based_on`` is the ServiceRequest id when the report fulfils an EHR order,
    which is what lets the EHR close the loop from order to result.
    ``partial`` marks a report that does not cover every ordered test (FHIR status ``partial``).
    """
    if not results:
        raise ValueError("a report needs at least one result")
    entries = [{"resource": observation(r),
                "request": {"method": "PUT", "url": f"Observation/{r.result_id}"}} for r in results]
    report = {
        "resourceType": "DiagnosticReport",
        "id": report_id,
        "status": ("partial" if partial else
                   "final" if all(r.status == "final" for r in results) else "preliminary"),
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                                  "code": "LAB"}]}],
        "code": {"text": "Laboratory report"},
        "specimen": [{"identifier": {"value": results[0].specimen_id}}],
        "result": [{"reference": f"Observation/{r.result_id}"} for r in results],
    }
    if results[0].patient_id:
        report["subject"] = {"identifier": {"value": results[0].patient_id}}
    if based_on:
        report["basedOn"] = [{"reference": f"ServiceRequest/{based_on}"}]
    entries.append({"resource": report,
                    "request": {"method": "PUT", "url": f"DiagnosticReport/{report_id}"}})
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def transaction(resources: list[dict]) -> dict:
    """Wrap resources in a transaction Bundle, one idempotent PUT per resource."""
    return {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
        for r in resources]}


def vital_signs(results: list[CanonicalResult]) -> dict:
    """Validated vital signs as individual vital-signs Observations.

    Blood pressure is sent as separate systolic and diastolic Observations in
    this version; the FHIR vital-signs profile prefers a 85354-9 panel with
    components, which is on the roadmap.
    """
    if not results:
        raise ValueError("no vital signs to chart")
    return transaction([observation(r) for r in results])


def task(task_id: str, status: str, *, focus: str, patient_id: str = "", description: str,
         status_reason: str = "", outputs: list[str] | None = None, code_text: str = "fulfill") -> dict:
    """A FHIR Task. Used for order fulfilment (focus ServiceRequest/<id>) and for
    work the lab asks of the ward, such as specimen recollection (focus Specimen)."""
    t: dict = {
        "resourceType": "Task",
        "id": task_id,
        "status": status,
        "intent": "order",
        "code": {"text": code_text},
        "description": description,
        "focus": {"reference": focus} if "/" in focus else {"identifier": {"value": focus}},
        "authoredOn": _now(),
        "lastModified": _now(),
    }
    if patient_id:
        t["for"] = {"identifier": {"value": patient_id}}
    if status_reason:
        t["statusReason"] = {"text": status_reason}
    if outputs:
        t["output"] = [{"type": {"text": "result"}, "valueReference": {"reference": o}} for o in outputs]
    return t


def communication(comm_id: str, *, patient_id: str, about: str, text: str, recipient: str) -> dict:
    """Documented notification of a critical result (who was told, what, when)."""
    return {
        "resourceType": "Communication",
        "id": comm_id,
        "status": "completed",
        "category": [{"text": "critical-result-notification"}],
        "priority": "stat",
        "subject": {"identifier": {"value": patient_id}},
        "about": [{"reference": about}],
        "recipient": [{"display": recipient}],
        "sent": _now(),
        "payload": [{"contentString": text}],
    }


REVIEW_TAG = "urn:mhs-bridge:review"
_VOLATILE = {"authoredOn", "lastModified", "sent", "meta"}      # set when a message is sent, not when it is drafted
_RECORD_TYPES = {"Provenance", "AuditEvent"}                    # records about a message, added at send time


def clinical_content(bundle: dict) -> list[dict]:
    """What an approval covers in a Bundle: its resources without send-time fields or records about the send.

    Recomputing this from what reached the EHR and comparing its hash with the approved one shows that what was
    sent is what a human approved.
    """
    return [{k: v for k, v in e["resource"].items() if k not in _VOLATILE} for e in bundle["entry"]
            if e["resource"]["resourceType"] not in _RECORD_TYPES]


def provenance(prov_id: str, targets: list[str], *, approver: str, at: str, request_id: str,
               payload_sha256: str, activity: str) -> dict:
    """Who authorised these resources and when: the approver's signature on the reviewed draft."""
    return {
        "resourceType": "Provenance",
        "id": prov_id,
        "target": [{"reference": t} for t in targets],
        "recorded": at,
        "activity": {"text": activity},
        "agent": [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                        "code": "verifier"}]},
                   "who": {"display": approver}}],
        "signature": [{"type": [{"system": "urn:iso-astm:E1762-95:2013", "code": "1.2.840.10065.1.12.1.5",
                                 "display": "Verification Signature"}],
                       "when": at, "who": {"display": approver}, "sigFormat": "application/sha256",
                       "data": payload_sha256}],
        "entity": [{"role": "source", "what": {"identifier": {"system": REVIEW_TAG, "value": request_id}}}],
    }


def audit_event(ae_id: str, *, action: str, at: str, who: str, what: str, request_id: str, payload_sha256: str,
                reason: str = "") -> dict:
    """One discrete step in a draft's lifecycle (drafted, approved, rejected, executed, ...)."""
    ae: dict = {
        "resourceType": "AuditEvent",
        "id": ae_id,
        "type": {"system": REVIEW_TAG, "code": "review", "display": "Human review of an outbound message"},
        "subtype": [{"system": REVIEW_TAG, "code": action}],
        "recorded": at,
        "outcome": "4" if action in ("EXECUTION_BLOCKED",) else "0",
        "agent": [{"who": {"display": who}, "requestor": not who.startswith("human:")}],
        "source": {"observer": {"display": "mhs-interop-bridge"}},
        "entity": [{"what": {"identifier": {"system": REVIEW_TAG, "value": request_id}}, "description": what,
                    "detail": [{"type": "payload_sha256", "valueString": payload_sha256}]}],
    }
    if reason:
        ae["outcomeDesc"] = reason
    return ae


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
