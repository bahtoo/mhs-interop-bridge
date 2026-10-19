"""The human operator channel.

Agents can raise requests; only humans can decide them. This is the one place
approvals are granted, escalations are read, and the emergency stop is cleared.
The agent tool surface can request an approval and check its status, and can
escalate, but has no tool to approve, deny or clear the stop.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ApprovalRequest:
    request_id: str
    app: str
    procedure: str
    args: dict
    requested_by: str
    status: str = "pending"          # pending, approved, denied, blocked
    decided_by: str = ""
    result: dict = field(default_factory=dict)


class OperatorChannel:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.apps: dict[str, Any] = {}
        self.approvals: dict[str, ApprovalRequest] = {}
        self.escalations: list[dict] = []
        self._ids = itertools.count(1)

    def attach(self, app: Any) -> None:
        self.apps[app.app_id] = app

    # --- agent side ---------------------------------------------------------------

    def request(self, app: str, procedure: str, args: dict, actor: str) -> str:
        req = ApprovalRequest(f"apr-{next(self._ids):04d}", app, procedure, dict(args), actor)
        self.approvals[req.request_id] = req
        return req.request_id

    def status(self, request_id: str) -> dict:
        req = self.approvals.get(request_id)
        return vars(req) if req else {"error": f"no approval request {request_id}"}

    def escalate(self, summary: str, actor: str = "agent") -> dict:
        self.escalations.append({"summary": summary, "by": actor,
                                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return {"status": "ok", "escalated": True}

    def pending(self) -> list[dict]:
        return [vars(r) for r in self.approvals.values() if r.status == "pending"]

    # --- human side ------------------------------------------------------------------

    def decide(self, request_id: str, approver: str, approve: bool) -> dict:
        req = self.approvals[request_id]
        if req.status != "pending":
            return {"status": "error", "error": f"{request_id} is already {req.status}"}
        req.decided_by = approver
        if not approve:
            req.status = "denied"
            return {"status": "ok", "request": "denied"}
        result = self.apps[req.app].execute_approved(req.procedure, req.args, approver)
        req.status = "approved" if result.get("status") == "ok" else "blocked"
        req.result = result
        return {"status": "ok", "request": req.status, "result": result}

    def clear_estop(self, operator: str) -> None:
        self.runtime.clear_estop(operator)
