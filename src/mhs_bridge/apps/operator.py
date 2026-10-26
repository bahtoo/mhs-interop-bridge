"""The human operator channel and its review queue.

Agents raise requests; only humans decide them. Every act a named human must own goes through here as a draft:
the outbound clinical messages (order download to an analyzer, release to the EHR, recollection Task to the
ward), a critical-value callback, charting vital signs, a proposed fix for missing data, and in copilot mode
every device invoke.

A draft is rendered when it is raised: what would be sent, a SHA-256 of its clinical content, the gates it
passed, and any data exceptions with their unverified suggestions. It waits in PENDING_REVIEW until a human
approves, rejects or revises it. Each decision is an ApprovalEvent: frozen, timestamped, and chained by hash to
the one before, so a changed or removed decision is detectable. An approved draft runs only if its gates still
pass and its content still renders to the approved hash; otherwise it ends EXECUTION_BLOCKED and nothing is sent.

The agent tool surface can raise a draft, read its status and escalate. Approve, reject, revise and clearing
the emergency stop exist only on the human side.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

PENDING, APPROVED, EXECUTED = "PENDING_REVIEW", "APPROVED", "EXECUTED"
REJECTED, BLOCKED, SUPERSEDED = "REJECTED", "EXECUTION_BLOCKED", "SUPERSEDED"

# The only legal moves. Transport is reachable from APPROVED alone, and only through decide().
TRANSITIONS = {PENDING: {APPROVED, REJECTED, SUPERSEDED}, APPROVED: {EXECUTED, BLOCKED}}

GENESIS = "0" * 64


class ReviewError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(content: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace)."""
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalEvent:
    event_id: str
    request_id: str
    revision: int
    payload_sha256: str
    decision: str          # approve, reject, revise
    approver: str
    at: str
    note: str
    prev_hash: str
    hash: str = ""

    def body(self) -> dict:
        d = asdict(self)
        d.pop("hash")
        return d

    def sealed(self) -> "ApprovalEvent":
        return replace(self, hash=content_hash(self.body()))


def verify_chain(events: list[ApprovalEvent] | list[dict]) -> str:
    """Empty string if the chain is intact, otherwise what is wrong with it."""
    prev = GENESIS
    for n, e in enumerate(events, 1):
        d = e if isinstance(e, dict) else asdict(e)
        if d.get("prev_hash") != prev:
            return f"event {n} ({d.get('event_id')}) does not follow the event before it"
        if d.get("hash") != content_hash({k: v for k, v in d.items() if k != "hash"}):
            return f"event {n} ({d.get('event_id')}) was changed after it was recorded"
        prev = d["hash"]
    return ""


@dataclass
class Draft:
    request_id: str
    app: str               # application id, or "mhs" for a device invoke drafted in copilot mode
    procedure: str
    args: dict
    requested_by: str
    kind: str = "request"
    created_at: str = field(default_factory=utc_now)
    status: str = PENDING
    revision: int = 1
    supersedes: str = ""
    superseded_by: str = ""
    intent: dict = field(default_factory=dict)       # what was asked for, unstructured: source record, rationale
    proposed: list = field(default_factory=list)     # the structured mapping: [{path, value, from, changed_in_revision}]
    payload: dict = field(default_factory=dict)      # {format, text, content}; content is what the hash covers
    payload_sha256: str = ""
    exceptions: list = field(default_factory=list)
    gates: dict = field(default_factory=dict)
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""
    result: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def move(self, status: str, by: str, reason: str = "") -> None:
        if status not in TRANSITIONS.get(self.status, set()):
            raise ReviewError(f"{self.request_id} cannot go from {self.status} to {status}")
        self.status = status
        self.history.append({"status": status, "at": utc_now(), "by": by, "reason": reason})

    def review(self) -> dict:
        """The reviewer's view: source intent and proposed mapping side by side, the payload, the exceptions."""
        return {"request_id": self.request_id, "revision": self.revision, "kind": self.kind, "status": self.status,
                "target": {"app": self.app, "procedure": self.procedure, "args": self.args},
                "requested_by": self.requested_by, "created_at": self.created_at,
                "intent": self.intent, "proposed": {"fields": self.proposed},
                "payload": {**self.payload, "sha256": self.payload_sha256},
                "exceptions": self.exceptions, "gates": self.gates,
                "decided_by": self.decided_by, "decided_at": self.decided_at, "note": self.note,
                "supersedes": self.supersedes, "superseded_by": self.superseded_by,
                "result": self.result, "history": self.history}

    def summary(self) -> dict:
        """The agent's view: enough to act on, without the whole payload."""
        return {"request_id": self.request_id, "kind": self.kind, "procedure": self.procedure, "status": self.status,
                "revision": self.revision, "superseded_by": self.superseded_by, "payload_sha256": self.payload_sha256,
                "exceptions": self.exceptions, "decided_by": self.decided_by, "decided_at": self.decided_at,
                "note": self.note, "result": self.result}


class OperatorChannel:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.apps: dict[str, Any] = {}
        self.approvals: dict[str, Draft] = {}
        self.events: list[ApprovalEvent] = []
        self.escalations: list[dict] = []
        self._ids = itertools.count(1)

    def attach(self, app: Any) -> None:
        self.apps[app.app_id] = app

    # --- agent side ---------------------------------------------------------------

    def request(self, app: str, procedure: str, args: dict, actor: str, draft: dict | None = None) -> str:
        """Queue a draft for review. Asking again for the same thing while it is pending returns the same draft."""
        same = next((d for d in self.approvals.values() if d.status == PENDING and d.app == app
                     and d.procedure == procedure and d.args == args), None)
        if same:
            return same.request_id
        d = Draft(f"apr-{next(self._ids):04d}", app, procedure, dict(args), actor, **(draft or {}))
        self._open(d, actor)
        return d.request_id

    def _open(self, d: Draft, by: str) -> None:
        d.payload_sha256 = content_hash(d.payload.get("content")) if d.payload else ""
        d.history.append({"status": PENDING, "at": d.created_at, "by": by, "reason": ""})
        self.approvals[d.request_id] = d
        self.runtime.record(by, "review", d.app, d.procedure, d.args, "drafted", reason=d.request_id)

    def status(self, request_id: str) -> dict:
        d = self.approvals.get(request_id)
        return d.summary() if d else {"error": f"no approval request {request_id}"}

    def escalate(self, summary: str, actor: str = "agent") -> dict:
        self.escalations.append({"summary": summary, "by": actor, "at": utc_now()})
        return {"status": "ok", "escalated": True}

    def pending(self) -> list[dict]:
        return [d.review() for d in self.approvals.values() if d.status == PENDING]

    def drafts(self) -> list[dict]:
        return [d.review() for d in self.approvals.values()]

    # --- human side ------------------------------------------------------------------

    def decide(self, request_id: str, approver: str, approve: bool, note: str = "") -> dict:
        d = self.approvals.get(request_id)
        if d is None:
            return {"status": "error", "error": f"no approval request {request_id}"}
        if d.status != PENDING:
            return {"status": "error", "error": f"{request_id} is {d.status}, not {PENDING}"}
        self._event(d, "approve" if approve else "reject", approver, note)
        d.decided_by, d.decided_at, d.note = approver, utc_now(), note
        if not approve:
            d.move(REJECTED, approver, note)
            return {"status": "ok", "request": d.status}
        d.move(APPROVED, approver, note)
        try:
            result = self.apps[d.app].execute_approved(d.procedure, d.args, approver, d)
        except ReviewError as exc:
            result = {"status": "blocked", "rule": "not_approved", "reason": str(exc)}
        d.result = result
        ok = result.get("status") == "ok"
        d.move(EXECUTED if ok else BLOCKED, approver, "" if ok else str(result.get("reason") or result.get("error")))
        return {"status": "ok", "request": d.status, "result": result}

    def revise(self, request_id: str, approver: str, changes: dict, note: str = "") -> dict:
        """Redline: the human changes arguments. The draft is rendered and gated again as a new revision."""
        d = self.approvals.get(request_id)
        if d is None:
            return {"status": "error", "error": f"no approval request {request_id}"}
        if d.status != PENDING:
            return {"status": "error", "error": f"{request_id} is {d.status}, not {PENDING}"}
        args = {**d.args, **changes}
        decision, draft = self.apps[d.app].prepare(d.procedure, args, d.requested_by)
        if not decision.allowed:
            return {"status": "blocked", "rule": decision.rule, "reason": decision.reason,
                    "note": f"{request_id} stays {PENDING} unchanged"}
        for f in draft.get("proposed", []):
            if f.get("from", "").removeprefix("args.") in changes:
                f["from"], f["changed_in_revision"] = f"revision {d.revision + 1} by {approver}", d.revision + 1
        new = Draft(f"apr-{next(self._ids):04d}", d.app, d.procedure, args, d.requested_by,
                    revision=d.revision + 1, supersedes=d.request_id, **draft)
        self._event(d, "revise", approver, note or f"changed {sorted(changes)}; now {new.request_id}")
        d.superseded_by = new.request_id
        d.move(SUPERSEDED, approver, note)
        self._open(new, approver)
        return {"status": "ok", "request_id": new.request_id, "supersedes": d.request_id}

    def clear_estop(self, operator: str) -> None:
        self.runtime.clear_estop(operator)

    # --- approval events -----------------------------------------------------------

    def _event(self, d: Draft, decision: str, approver: str, note: str) -> ApprovalEvent:
        prev = self.events[-1].hash if self.events else GENESIS
        e = ApprovalEvent(f"evt-{len(self.events) + 1:04d}", d.request_id, d.revision, d.payload_sha256, decision,
                          approver, utc_now(), note, prev).sealed()
        self.events.append(e)
        self.runtime.record(approver, "review", d.app, d.procedure, d.args, decision, reason=e.event_id)
        return e

    def authorise(self, d: Draft) -> ApprovalEvent:
        """The approval event that lets this draft reach its transport. Raises if there is none."""
        if d.status != APPROVED:
            raise ReviewError(f"{d.request_id} is {d.status}, not {APPROVED}")
        problem = verify_chain(self.events)
        if problem:
            raise ReviewError(f"approval events do not verify: {problem}")
        e = next((e for e in reversed(self.events) if e.request_id == d.request_id and e.decision == "approve"), None)
        if e is None or e.payload_sha256 != d.payload_sha256 or not e.approver.startswith("human:"):
            raise ReviewError(f"no human approval event for {d.request_id} at sha256 {d.payload_sha256[:12]}")
        return e

    def verify_chain(self) -> str:
        return verify_chain(self.events)

    def audit_events(self) -> list[dict]:
        """Every draft's lifecycle as FHIR AuditEvents, for an EHR audit repository."""
        from ..protocols import fhir
        out = []
        for d in self.approvals.values():
            for n, h in enumerate(d.history, 1):
                out.append(fhir.audit_event(f"ae-{d.request_id}-{n}", action=h["status"], at=h["at"],
                                            who=h["by"], what=f"{d.app}.{d.procedure}", request_id=d.request_id,
                                            payload_sha256=d.payload_sha256, reason=h["reason"]))
        return out
