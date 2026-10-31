"""The stand-in MHS runtime: only the publicly described behaviour, plus marked proposals."""

from mhs_bridge.environment import build
from mhs_bridge.tools import TOOL_SPECS


def test_discovery_lists_every_device_with_risk_class():
    devices = {d["device_id"]: d for d in build().runtime.discover()}
    assert set(devices) == {"lh-01", "arm-01", "reader-01", "chem-01", "chem-02", "hema-01", "mon-12"}
    assert devices["mon-12"]["risk_class"] == "patient_connected"
    assert list(devices)[:3] == ["arm-01", "lh-01", "reader-01"]        # research devices first


def test_manifest_limits_and_set_constraints_are_enforced():
    rt = build().runtime
    r = rt.invoke("lh-01", "Transfer", {"Source": "RES:BUF", "Target": "P1:A1", "Volume": 250, "FlowRate": 10})
    assert r["rule"] == "limit" and "Split" in r["hint"]
    r = rt.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "incubator"})
    assert r["rule"] == "limit" and "not one of" in r["reason"]


def test_patient_connected_device_refuses_every_invoke():
    rt = build().runtime
    assert rt.invoke("mon-12", "silence_alarm", {})["rule"] == "read_only_device"
    assert rt.manifest("mon-12").procedures == []


def test_emergency_stop_any_actor_engages_only_human_clears():
    env = build()
    env.runtime.engage_estop("agent", "spill")
    assert env.runtime.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "reader-01"})["rule"] == "emergency_stop"
    assert not {"mhs_clear_estop", "operator_approve", "operator_clear_estop"} & {t["name"] for t in TOOL_SPECS}
    env.operator.clear_estop("human:operator")
    assert env.runtime.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "reader-01"})["status"] == "ok"


def test_routine_steps_are_checked_individually_and_stop_at_first_failure():
    env = build()
    r = env.runtime.invoke("lh-01", "SerialDilution", {"Plate": "P1", "Row": "A", "Stock": "RES:BSA", "TopFraction": 1.0,
                                                       "Steps": 8, "FlowRate": 140, "MixCycles": 5})
    assert r["status"] == "error" and r["error"] == "FoamDetected" and r["completed_steps"] == 0
    assert len(env.world.transfers) == 1


def test_offline_device_cannot_be_invoked_and_audit_records_everything():
    env = build({"reader-01": {"offline": True}})
    assert env.runtime.invoke("reader-01", "ReadPlate", {"Plate": "P1", "WavelengthNm": 562})["rule"] == "device_unreachable"
    assert env.runtime.read("reader-01", "instrument_status") == {"error": "reader-01 is offline"}
    kinds = {(e.kind, e.outcome) for e in env.runtime.audit}
    assert ("invoke", "blocked") in kinds and ("read", "error") in kinds
