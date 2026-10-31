"""FHIR ServiceRequest intake and capability routing over MHS discovery.

Routing reads each analyzer's ``catalogue`` state through MHS (vendor code to
LOINC) and assigns each requested LOINC code to the first analyzer in service
(online and not stopped on a fault) that offers it. Codes nobody offers are
recorded as unroutable and never substituted.

A request that is malformed is rejected. A request that is well formed but lacks
the specimen accession is kept, with a data exception and candidate accessions
taken from the request itself; it cannot be dispatched until a human confirms one.
"""

from __future__ import annotations

from typing import Any

from ...protocols.exceptions import DataException, Suggestion
from .models import Order

LOINC = "http://loinc.org"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"     # identifier types; ACSN is an accession number


class OrderError(ValueError):
    pass


def parse_service_request(sr: dict) -> Order:
    if sr.get("resourceType") != "ServiceRequest":
        raise OrderError("not a ServiceRequest")
    if sr.get("status") != "active":
        raise OrderError(f"ServiceRequest status is {sr.get('status')!r}, expected 'active'")
    if sr.get("intent") not in ("order", "original-order"):
        raise OrderError(f"ServiceRequest intent {sr.get('intent')!r} is not an order")
    codes = [c["code"] for c in sr.get("code", {}).get("coding", []) if c.get("system") == LOINC]
    if not codes:
        raise OrderError("no LOINC-coded test in ServiceRequest.code")
    patient = sr.get("subject", {}).get("identifier", {}).get("value", "")
    if not patient:
        raise OrderError("ServiceRequest.subject.identifier is required")
    specimens = sr.get("specimen", [])
    specimen = specimens[0].get("identifier", {}).get("value", "") if specimens else ""
    order = Order(sr["id"], patient, specimen, list(dict.fromkeys(codes)), sr.get("priority", "routine"),
                  f"task-{sr['id']}", source=sr)
    if not specimen:
        order.exceptions.append(missing_accession(sr).to_dict())
    return order


def missing_accession(sr: dict) -> DataException:
    """Candidate accessions from the request itself. Each one cites the field it came from; none is applied."""
    found: list[Suggestion] = []
    for n, ident in enumerate(sr.get("identifier", [])):
        if any(c.get("system") == V2_0203 and c.get("code") == "ACSN" for c in ident.get("type", {}).get("coding", [])):
            found.append(Suggestion(ident.get("value", ""), "the request carries an accession-type identifier",
                                    [f"ServiceRequest.identifier[{n}] type ACSN"]))
    if sr.get("requisition", {}).get("value"):
        found.append(Suggestion(sr["requisition"]["value"], "the requisition (paper form) number; labs sometimes "
                                "label specimens with it, so check the tube", ["ServiceRequest.requisition"],
                                plausible=False))
    found.append(Suggestion(None, "no usable candidate: read the barcode on the tube and enter it", ["specimen label"],
                            plausible=False))
    return DataException(f"exc-{sr['id']}-specimen", "specimen_id", "missing_accession",
                         "ServiceRequest.specimen has no identifier, so the order cannot be matched to a tube", found)


def in_service(runtime: Any, device_id: str, actor: str = "app:clinical") -> bool:
    try:
        return runtime.read(device_id, "mode", actor=actor)["mode"] != "error"
    except (KeyError, TypeError):
        return True                     # an analyzer that reports no mode is taken as in service


def route(order: Order, runtime: Any, actor: str = "app:clinical") -> Order:
    analyzers = [d for d in runtime.discover() if d["device_type"].endswith("analyzer") and d["online"]
                 and in_service(runtime, d["device_id"], actor)]
    catalogues = {d["device_id"]: runtime.read(d["device_id"], "catalogue", actor=actor) for d in analyzers}
    order.routing, order.unroutable = {}, []
    for code in order.loinc_codes:
        target = next(((dev, vendor) for dev, cat in catalogues.items()
                       for vendor, loinc in cat.items() if loinc == code), None)
        if target is None:
            order.unroutable.append(code)
        else:
            order.routing.setdefault(target[0], []).append(target[1])
    return order
