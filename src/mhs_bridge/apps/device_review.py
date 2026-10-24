"""Copilot mode: device invokes drafted for a human's review instead of executed.

In the default (autonomous) mode an agent's ``mhs_invoke`` runs at once, behind the runtime's limits and the
drivers' interlocks. In copilot mode the same call runs those checks without acting (``Runtime.preview``) and,
if they pass, queues a ``device_invoke`` draft. On approval the invoke runs under the approver's name, so every
limit and interlock is checked again at the moment the hardware would move.

The emergency stop is never drafted: anyone may engage it at once, in every mode. Reads and checks stay free.
"""

from __future__ import annotations

import json
from typing import Any

from ..mhs_shim import Decision


class DeviceReview:
    app_id = "mhs"

    def __init__(self, runtime: Any, operator: Any):
        self.runtime, self.operator = runtime, operator
        operator.attach(self)

    def invoke(self, device_id: str, procedure: str, args: dict, actor: str = "agent", rationale: str = "") -> dict:
        target = f"{device_id}.{procedure}"
        d, draft = self.prepare(target, args, actor, rationale)
        if not d.allowed:
            self.runtime.record(actor, "invoke", device_id, procedure, args, "blocked", d.rule, d.reason)
            return {"status": "blocked", "rule": d.rule, "reason": d.reason, "hint": d.hint}
        rid = self.operator.request(self.app_id, target, args, actor, draft)
        req = self.operator.approvals[rid]
        return {"status": "needs_approval", "request_id": rid, "kind": req.kind, "payload_sha256": req.payload_sha256,
                "reason": "copilot mode: drafted for review (PENDING_REVIEW). The invoke runs, with every check "
                          "repeated, when a named human approves it; poll operator_approval_status"}

    def prepare(self, target: str, args: dict, actor: str = "agent", rationale: str = "",
                prior: Any = None) -> tuple[Decision, dict]:
        device_id, procedure = target.split(".", 1)
        pre = self.runtime.preview(device_id, procedure, args, actor=actor)
        if not pre["allowed"]:
            return Decision.block(pre["rule"], pre["reason"], pre.get("hint", "")), {}
        proc = self.runtime.manifest(device_id).procedure(procedure)
        params = {p.name: p for p in proc.params}
        proposed = []
        for k, v in args.items():
            p = params.get(k)
            limit = (f"; enforced {p.min} to {p.max} {p.unit}".rstrip() if p is not None and
                     (p.min is not None or p.max is not None) else "")
            proposed.append({"path": f"{target}.{k}", "value": v, "from": f"args.{k}{limit}"})
        return Decision.ok(), {
            "kind": "device_invoke",
            "intent": {"source": {"type": "agent_request", "id": target, "text": rationale or "(no rationale given)",
                                  "raw": {}}, "agent_rationale": rationale},
            "proposed": proposed,
            "payload": {"format": "mhs_invoke", "text": f"{target}({json.dumps(args)})",
                        "content": {"device_id": device_id, "procedure": procedure, "args": args}},
            "gates": {"allowed": True, "checked": ["risk class", "emergency stop", "manifest limits", "reachability",
                                                   "driver interlocks"], **({"note": pre["note"]} if "note" in pre else {})},
        }

    def execute_approved(self, target: str, args: dict, approver: str, draft: Any) -> dict:
        self.operator.authorise(draft)
        device_id, procedure = target.split(".", 1)
        # The approved content is the call itself; the runtime repeats every check before anything moves.
        return self.runtime.invoke(device_id, procedure, draft.payload["content"]["args"], actor=approver)
