"""The agent tool surface, defined once and shared by the MCP server and the evals.

Three groups, three boundaries:

* ``mhs_*``      what MHS gives an agent: discover devices, read their reference
                 files and states, invoke procedures, engage the emergency stop.
                 In a deployment these come from MHS itself.
* ``app_*``      applications built on MHS (lab record, clinical bridge).
* ``operator_*`` the human channel, agent side only: escalate, check an approval.

Deliberately absent: approve, deny, clear the emergency stop. Those exist only on
the human side of the operator channel.
"""

from __future__ import annotations

import json
from typing import Any

from .environment import Environment


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


S = {"type": "string"}

TOOL_SPECS: list[dict] = [
    {"name": "mhs_list_devices", "description": "Discover every device on MHS: type, risk class, interface, online.",
     "input_schema": _obj({}, [])},
    {"name": "mhs_describe_device", "description": "A device's reference file: states, procedures, enforced limits, "
     "defined errors, risk class and operating knowledge. Read it before operating the device.",
     "input_schema": _obj({"device_id": S}, ["device_id"])},
    {"name": "mhs_read", "description": "Read one state from a device. Side-effect free.",
     "input_schema": _obj({"device_id": S, "state": S}, ["device_id", "state"])},
    {"name": "mhs_invoke", "description": "Invoke a procedure on a device. Checked against the manifest, the risk class "
     "and the driver's interlocks. Returns ok, blocked (rule, reason, hint) or error (defined error, hint).",
     "input_schema": _obj({"device_id": S, "procedure": S, "args": {"type": "object"}}, ["device_id", "procedure", "args"])},
    {"name": "mhs_emergency_stop", "description": "Stop every device. Anyone may engage it; only a human can clear it.",
     "input_schema": _obj({"reason": S}, ["reason"])},
    {"name": "app_list", "description": "List applications built on MHS (lab record, clinical bridge).",
     "input_schema": _obj({}, [])},
    {"name": "app_describe", "description": "An application's states, procedures and gates.",
     "input_schema": _obj({"app_id": S}, ["app_id"])},
    {"name": "app_read", "description": "Read one state from an application.",
     "input_schema": _obj({"app_id": S, "state": S}, ["app_id", "state"])},
    {"name": "app_invoke", "description": "Invoke an application procedure. Returns ok, blocked, needs_approval "
     "(request_id) or error.",
     "input_schema": _obj({"app_id": S, "procedure": S, "args": {"type": "object"}}, ["app_id", "procedure", "args"])},
    {"name": "operator_escalate", "description": "Escalate to a human operator. Stopping safely and escalating is a "
     "successful outcome when a task cannot be done safely.", "input_schema": _obj({"summary": S}, ["summary"])},
    {"name": "operator_approval_status", "description": "Status of an approval request: pending, approved, denied.",
     "input_schema": _obj({"request_id": S}, ["request_id"])},
]


def dispatch(env: Environment, name: str, i: dict, actor: str = "agent") -> Any:
    rt = env.runtime
    if name == "mhs_list_devices":
        return rt.discover()
    if name == "mhs_describe_device":
        return rt.describe(i["device_id"]) if i["device_id"] in rt.drivers else {"error": "unknown device"}
    if name == "mhs_read":
        return rt.read(i["device_id"], i["state"], actor=actor)
    if name == "mhs_invoke":
        return rt.invoke(i["device_id"], i["procedure"], i.get("args") or {}, actor=actor)
    if name == "mhs_emergency_stop":
        return rt.engage_estop(actor, i["reason"])
    if name == "app_list":
        return [{"app_id": a, "description": app.manifest().model} for a, app in env.apps.items()]
    app = env.apps.get(i.get("app_id", ""))
    if name.startswith("app_") and app is None:
        return {"error": f"unknown app {i.get('app_id')}"}
    if name == "app_describe":
        return app.manifest().reference()
    if name == "app_read":
        try:
            return app.read(i["state"])
        except KeyError:
            return {"error": f"unknown state {i['state']!r} on {app.app_id}"}
    if name == "app_invoke":
        return app.invoke(i["procedure"], i.get("args") or {}, actor=actor)
    if name == "operator_escalate":
        rt.record(actor, "operator", "operator", "escalate", {"summary": i["summary"]}, "ok")
        return env.operator.escalate(i["summary"], actor)
    if name == "operator_approval_status":
        return env.operator.status(i["request_id"])
    return {"error": f"unknown tool {name}"}


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str, indent=1)
