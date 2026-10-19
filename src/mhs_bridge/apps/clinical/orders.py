"""FHIR ServiceRequest intake and capability routing over MHS discovery.

Routing reads each analyzer's ``catalogue`` state through MHS (vendor code to
LOINC) and assigns each requested LOINC code to the first online analyzer that
offers it. Codes nobody offers are recorded as unroutable and never substituted.
"""

from __future__ import annotations

from typing import Any

from .models import Order

LOINC = "http://loinc.org"


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
    if not specimen:
        raise OrderError("ServiceRequest.specimen identifier (barcode) is required")
    return Order(sr["id"], patient, specimen, list(dict.fromkeys(codes)), sr.get("priority", "routine"),
                 f"task-{sr['id']}")


def route(order: Order, runtime: Any, actor: str = "app:clinical") -> Order:
    analyzers = [d for d in runtime.discover() if d["device_type"].endswith("analyzer") and d["online"]]
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
