"""MCP server exposing the stand-in MHS runtime, the apps and the operator channel.

In a deployment the ``mhs_*`` tools come from MHS itself, and the applications
run as their own MCP server. Here one process serves all three groups so the
whole system can be tried from Claude Code or any MCP client.

Three parties, three channels:

    agent    MCP tools (this server)
    human    mhs-bridge approvals / approve / deny / clear-estop, through <state>/decisions.jsonl
    EHR      FHIR ServiceRequests dropped into <state>/inbox/ (mhs-bridge submit-order)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .environment import Environment, build
from .evals.scenarios import BY_NAME
from .tools import dispatch, render

INSTRUCTIONS = ("Laboratory instruments on the Model Hardware Standard (stand-in runtime), plus applications built on "
                "it. Start with mhs_list_devices, read mhs_describe_device before operating a device, and treat "
                "blocked calls as information. You can request human approvals but not grant them.")


class FileOperatorChannel:
    """File-backed human and EHR channels for a running server."""

    def __init__(self, env: Environment, state_dir: str | Path):
        self.env = env
        self.dir = Path(state_dir)
        (self.dir / "inbox").mkdir(parents=True, exist_ok=True)
        self.decisions = self.dir / "decisions.jsonl"
        self._applied = 0

    def sync(self) -> None:
        for f in sorted((self.dir / "inbox").glob("*.json")):
            res = self.env.clinical.receive_order(json.loads(f.read_text(encoding="utf-8")))
            f.rename(f.with_suffix(".received" if res.get("status") == "ok" else ".rejected"))
        if self.decisions.exists():
            lines = self.decisions.read_text(encoding="utf-8").splitlines()
            for line in lines[self._applied:]:
                d = json.loads(line)
                if d.get("type") == "clear_estop":
                    self.env.operator.clear_estop(d["approver"])
                elif d.get("request_id") in self.env.operator.approvals:
                    self.env.operator.decide(d["request_id"], d["approver"], bool(d.get("approve", True)))
            self._applied = len(lines)
        (self.dir / "pending.json").write_text(json.dumps(self.env.operator.pending(), indent=1, default=str),
                                               encoding="utf-8")
        (self.dir / "escalations.json").write_text(json.dumps(self.env.operator.escalations, indent=1),
                                                   encoding="utf-8")


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer      # MCP Python SDK 2.x
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP          # MCP Python SDK 1.x
        return FastMCP


def build_server(scenario: str | None = None, state_dir: str | Path = ".mhs_state", audit_path: str | None = None):
    sc = BY_NAME[scenario] if scenario else None
    env = build(sc.faults if sc else {}, folder=Path(state_dir) / "instruments", audit_path=audit_path)
    for sr in (sc.ehr_orders if sc else []):
        env.clinical.receive_order(sr)
    channel = FileOperatorChannel(env, state_dir)
    server = _server_class()(name="mhs-interop-bridge", instructions=INSTRUCTIONS)

    def call(name: str, **kw: Any) -> str:
        channel.sync()
        out = dispatch(env, name, kw)
        channel.sync()
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
    def mhs_read(device_id: str, state: str) -> str:
        """Read one state from a device. Side-effect free."""
        return call("mhs_read", device_id=device_id, state=state)

    @server.tool()
    def mhs_invoke(device_id: str, procedure: str, args: dict[str, Any] | None = None) -> str:
        """Invoke a procedure on a device. Checked against limits, risk class and driver interlocks."""
        return call("mhs_invoke", device_id=device_id, procedure=procedure, args=args or {})

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
    def app_invoke(app_id: str, procedure: str, args: dict[str, Any] | None = None) -> str:
        """Invoke an application procedure. Returns ok, blocked, needs_approval or error."""
        return call("app_invoke", app_id=app_id, procedure=procedure, args=args or {})

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
        """Status of an approval request."""
        return call("operator_approval_status", request_id=request_id)

    return server, env


def main(scenario: str | None = None, state_dir: str = ".mhs_state", audit_path: str | None = None) -> None:
    server, _ = build_server(scenario, state_dir, audit_path)
    server.run("stdio")
