"""MCP server exposing the stand-in MHS runtime, the apps and the operator channel.

In a deployment the ``mhs_*`` tools come from MHS itself, and the applications
run as their own MCP server. Here one process serves all three groups so the
whole system can be tried from Claude Code or any MCP client.

Three parties, three channels:

    agent    MCP tools (this server)
    human    mhs-bridge drafts / show / approve / deny / revise / clear-estop, through <state>/decisions.jsonl
    EHR      FHIR ServiceRequests dropped into <state>/inbox/ (mhs-bridge submit-order)

What the server writes for the human side, in <state>/:

    pending.json        drafts in PENDING_REVIEW, each with intent, proposed mapping, payload and exceptions
    drafts.json         every draft and its history
    review.jsonl        the approval events, append-only and hash-chained; read back and verified on every sync
    review_status.json  whether the chain verifies; if it does not, no further decision is applied
    audit_events.json   every draft's lifecycle as FHIR AuditEvents
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .apps.operator import ApprovalEvent, verify_chain
from .environment import Environment, build
from .evals.scenarios import BY_NAME
from .tools import dispatch, image_caption, is_image, render

INSTRUCTIONS = ("Laboratory instruments on the Model Hardware Standard (stand-in runtime), plus applications built on "
                "it. Start with mhs_list_devices, read mhs_describe_device before operating a device, and treat "
                "blocked calls as information. Clinical messages are drafted for a named human's review "
                "(needs_approval); poll operator_approval_status. You can request human approvals but not grant them.")


class FileOperatorChannel:
    """File-backed human and EHR channels for a running server."""

    def __init__(self, env: Environment, state_dir: str | Path):
        self.env = env
        self.dir = Path(state_dir)
        (self.dir / "inbox").mkdir(parents=True, exist_ok=True)
        self.decisions = self.dir / "decisions.jsonl"
        self.review_log = self.dir / "review.jsonl"
        self._applied = 0
        # A log from an earlier session is kept and continued: new events chain onto its last hash.
        earlier = self._logged()
        self.problem = verify_chain(earlier)
        if not self.problem:
            env.operator.events.extend(ApprovalEvent(**e) for e in earlier)
        self._written = len(earlier)

    def _logged(self) -> list[dict]:
        if not self.review_log.exists():
            return []
        return [json.loads(line) for line in self.review_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _check_log(self) -> str:
        """The file must still hold exactly the events this server wrote, and they must still chain."""
        logged = self._logged()
        if logged[:self._written] != [vars(e) for e in self.env.operator.events[:self._written]]:
            return "review.jsonl no longer matches the approval events this server recorded"
        return verify_chain(logged)

    def sync(self) -> None:
        for f in sorted((self.dir / "inbox").glob("*.json")):
            res = self.env.clinical.receive_order(json.loads(f.read_text(encoding="utf-8")))
            f.rename(f.with_suffix(".received" if res.get("status") == "ok" else ".rejected"))
        self.problem = self.problem or self._check_log()
        if self.decisions.exists() and not self.problem:      # a broken log stops every further decision
            lines = self.decisions.read_text(encoding="utf-8").splitlines()
            op = self.env.operator
            for line in lines[self._applied:]:
                d = json.loads(line)
                if d.get("type") == "clear_estop":
                    op.clear_estop(d["approver"])
                elif d.get("type") == "revise" and d.get("request_id") in op.approvals:
                    op.revise(d["request_id"], d["approver"], d.get("set", {}), d.get("note", ""))
                elif d.get("request_id") in op.approvals:
                    op.decide(d["request_id"], d["approver"], bool(d.get("approve", True)), d.get("note", ""))
            self._applied = len(lines)
        new = self.env.operator.events[self._written:]
        if new and not self.problem:
            with self.review_log.open("a", encoding="utf-8") as fh:
                for e in new:
                    fh.write(json.dumps(vars(e)) + "\n")
            self._written += len(new)
        self._write("pending.json", self.env.operator.pending())
        self._write("drafts.json", self.env.operator.drafts())
        self._write("escalations.json", self.env.operator.escalations)
        self._write("audit_events.json", self.env.operator.audit_events())
        self._write("review_status.json", {"chain": "broken" if self.problem else "ok", "problem": self.problem,
                                           "events": self._written, "mode": self.env.mode})

    def _write(self, name: str, value: Any) -> None:
        (self.dir / name).write_text(json.dumps(value, indent=1, default=str), encoding="utf-8")


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer      # MCP Python SDK 2.x
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP          # MCP Python SDK 1.x
        return FastMCP


def build_server(scenario: str | None = None, state_dir: str | Path = ".mhs_state", audit_path: str | None = None,
                 mode: str = "autonomous"):
    sc = BY_NAME[scenario] if scenario else None
    env = build(sc.faults if sc else {}, folder=Path(state_dir) / "instruments", audit_path=audit_path, mode=mode)
    for sr in (sc.ehr_orders if sc else []):
        env.clinical.receive_order(sr)
    channel = FileOperatorChannel(env, state_dir)
    note = " Copilot mode: every device invoke is also drafted for review before it runs." if mode == "copilot" else ""
    server = _server_class()(name="mhs-interop-bridge", instructions=INSTRUCTIONS + note)

    def call(name: str, **kw: Any) -> Any:
        channel.sync()
        out = dispatch(env, name, kw)
        channel.sync()
        if is_image(out):
            try:
                from mcp.server.fastmcp import Image
                import base64
                return [Image(data=base64.b64decode(out["data_base64"]), format="png"), image_caption(out)]
            except ImportError:      # an SDK without Image: say what there is rather than send base64 as text
                return image_caption(out) + "\n(image not shown: this MCP SDK cannot return images)"
        return render(env, out)

    @server.tool()
    def mhs_list_devices() -> str:
        """Discover every device on MHS: type, risk class, interface, online."""
        return call("mhs_list_devices")

    @server.tool()
    def mhs_describe_device(device_id: str) -> str:
        """A device's reference file: states, procedures, limits, defined errors, risk class, operating knowledge."""
        return call("mhs_describe_device", device_id=device_id)

    @server.tool()
    def mhs_read(device_id: str, state: str) -> Any:
        """Read one state from a device. Side-effect free. Image states (a plate photo) come back as images."""
        return call("mhs_read", device_id=device_id, state=state)

    @server.tool()
    def mhs_invoke(device_id: str, procedure: str, args: dict[str, Any] | None = None, rationale: str = "") -> str:
        """Invoke a procedure on a device. Checked against limits, risk class and driver interlocks. In copilot mode
        it is drafted for a human's review instead (needs_approval)."""
        return call("mhs_invoke", device_id=device_id, procedure=procedure, args=args or {}, rationale=rationale)

    @server.tool()
    def mhs_check(device_id: str, procedure: str, args: dict[str, Any] | None = None) -> str:
        """Would this invoke be allowed, and if not, why? Same checks as mhs_invoke, without doing anything."""
        return call("mhs_check", device_id=device_id, procedure=procedure, args=args or {})

    @server.tool()
    def mhs_emergency_stop(reason: str) -> str:
        """Stop every device. Anyone may engage it; only a human can clear it."""
        return call("mhs_emergency_stop", reason=reason)

    @server.tool()
    def app_list() -> str:
        """List applications built on MHS."""
        return call("app_list")

    @server.tool()
    def app_describe(app_id: str) -> str:
        """An application's states, procedures and gates."""
        return call("app_describe", app_id=app_id)

    @server.tool()
    def app_read(app_id: str, state: str) -> str:
        """Read one state from an application."""
        return call("app_read", app_id=app_id, state=state)

    @server.tool()
    def app_invoke(app_id: str, procedure: str, args: dict[str, Any] | None = None, rationale: str = "") -> str:
        """Invoke an application procedure. Returns ok, blocked, needs_approval or error. Clinical bridge
        procedures are drafted for a named human's review."""
        return call("app_invoke", app_id=app_id, procedure=procedure, args=args or {}, rationale=rationale)

    @server.tool()
    def app_check(app_id: str, procedure: str, args: dict[str, Any] | None = None) -> str:
        """Would this application procedure be allowed, or need a human's approval? Same gates, nothing done."""
        return call("app_check", app_id=app_id, procedure=procedure, args=args or {})

    @server.tool()
    def operator_escalate(summary: str) -> str:
        """Escalate to a human operator."""
        return call("operator_escalate", summary=summary)

    @server.tool()
    def operator_approval_status(request_id: str) -> str:
        """Status of a drafted request: PENDING_REVIEW, EXECUTED, EXECUTION_BLOCKED, REJECTED or SUPERSEDED."""
        return call("operator_approval_status", request_id=request_id)

    return server, env


def main(scenario: str | None = None, state_dir: str = ".mhs_state", audit_path: str | None = None,
         mode: str = "autonomous") -> None:
    server, _ = build_server(scenario, state_dir, audit_path, mode)
    server.run("stdio")
