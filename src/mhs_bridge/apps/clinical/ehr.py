"""Where FHIR goes: an in-memory EHR for evals, or a real FHIR R4 server."""

from __future__ import annotations

import json
from typing import Protocol


class EhrSink(Protocol):
    def send(self, bundle: dict) -> dict: ...


class MemoryEhr:
    def __init__(self) -> None:
        self.bundles: list[dict] = []

    def send(self, bundle: dict) -> dict:
        self.bundles.append(bundle)
        return {"status": "accepted", "entries": len(bundle["entry"])}

    def resources(self, resource_type: str) -> list[dict]:
        return [e["resource"] for b in self.bundles for e in b["entry"] if e["resource"]["resourceType"] == resource_type]


class HttpFhirSink:
    """POST transaction Bundles to a FHIR R4 base URL (for example a local HAPI FHIR server)."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def send(self, bundle: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(self.base_url, data=json.dumps(bundle).encode(),
                                     headers={"Content-Type": "application/fhir+json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:   # noqa: S310
            return {"status": resp.status}
