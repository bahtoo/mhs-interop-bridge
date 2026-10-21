"""Applications on MHS: the lab record and the clinical bridge, and the operator channel."""

import json

import pytest

from mhs_bridge.apps.clinical.orders import OrderError, parse_service_request, route
from mhs_bridge.environment import build
from mhs_bridge.evals.scenarios import SR_CHEM, SR_HEMA, SR_TSH


def _qc(env, dev, tests):
    for t in tests:
        assert env.runtime.invoke(dev, "run_qc", {"test": t, "level": "L1"})["status"] == "ok"


def _run(env, dev, spec, tests):
    return env.runtime.invoke(dev, "run_specimen", {"specimen_id": spec, "tests": tests})


# --- lab record ------------------------------------------------------------------------

def _dilute_and_read(env, row, stock, fraction, flow, mix):
    rt = env.runtime
    rt.invoke("lh-01", "SerialDilution", {"Plate": "P1", "Row": row, "Stock": stock, "TopFraction": fraction,
                                          "Steps": 8, "FlowRate": flow, "MixCycles": mix})
    rt.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "reader-01"})
    rt.invoke("reader-01", "ReadPlate", {"Plate": "P1", "WavelengthNm": 562})
    rt.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "lh-01"})


def test_lab_record_refuses_saturated_and_accepts_compressed_range():
    env = build()
    _dilute_and_read(env, "A", "RES:DYE", 1.0, 140, 3)
    r = env.lab.invoke("accept_series", {"plate": "P1", "row": "A"})
    assert r["rule"] == "data_integrity" and "saturated" in r["reason"]
    _dilute_and_read(env, "B", "RES:DYE", 0.5, 140, 3)
    assert env.lab.invoke("accept_series", {"plate": "P1", "row": "B"})["status"] == "ok"
    assert env.lab.accepted[0]["r2"] > 0.99


def test_lab_record_refuses_rows_with_foamed_wells():
    env = build()
    _dilute_and_read(env, "A", "RES:BSA", 0.5, 140, 0)        # foams at column 1, routine stops there
    fit = env.lab.read("series_fit:P1:A")
    assert fit["invalid_wells"] == ["A1"]
    assert env.lab.invoke("accept_series", {"plate": "P1", "row": "A"})["rule"] == "data_integrity"


def test_lab_record_keeps_the_method_from_the_audit_and_the_deviation_note():
    env = build()
    _dilute_and_read(env, "A", "RES:BSA", 0.5, 10, 3)
    _dilute_and_read(env, "B", "RES:DYE", 0.5, 140, 5)
    note = "Protocol said 140 uL/s; used 10 uL/s for viscous BSA per the device note."
    assert env.lab.invoke("accept_series", {"plate": "P1", "row": "A", "note": note})["status"] == "ok"
    rec = env.lab.read("accepted")[0]
    assert rec["note"] == note and rec["recorded_by"] == "agent"
    assert rec["method"]["flow_rates_ul_s"] == [10.0]       # row B's 140 uL/s transfers are not mixed in
    assert rec["method"]["transfers"] == 16


# --- orders --------------------------------------------------------------------------------

def test_service_request_validation():
    with pytest.raises(OrderError, match="active"):
        parse_service_request({**SR_CHEM, "status": "draft"})
    with pytest.raises(OrderError, match="specimen"):
        parse_service_request({k: v for k, v in SR_CHEM.items() if k != "specimen"})


def test_routing_reads_analyzer_catalogues_through_mhs():
    env = build()
    assert route(parse_service_request(SR_HEMA), env.runtime).routing == {"hema-01": ["WBC", "HGB", "PLT"]}
    tsh = route(parse_service_request(SR_TSH), env.runtime)
    assert tsh.routing == {"chem-01": ["GLU"]} and tsh.unroutable == ["3016-3"]
    assert any(e.target == "chem-01" and e.action == "catalogue" for e in env.runtime.audit)


def test_order_to_result_closes_the_loop():
    env = build()
    env.clinical.receive_order(SR_CHEM)
    assert env.clinical.invoke("dispatch_order", {"order_id": "sr-1001"})["status"] == "ok"
    _qc(env, "chem-01", ["GLU", "K"])
    _run(env, "chem-01", "S-1001", ["GLU", "K"])
    assert env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["based_on"] == "sr-1001"
    report = env.ehr.resources("DiagnosticReport")[0]
    assert report["basedOn"] == [{"reference": "ServiceRequest/sr-1001"}]
    assert [t["status"] for t in env.ehr.resources("Task")] == ["requested", "accepted", "in-progress", "completed"]
    assert env.ehr.resources("Task")[-1]["output"][0]["valueReference"]["reference"] == f"DiagnosticReport/{report['id']}"


def test_unroutable_order_and_undispatched_results_are_refused():
    env = build()
    env.clinical.receive_order(SR_TSH)
    assert env.clinical.invoke("dispatch_order", {"order_id": "sr-3001"})["rule"] == "unroutable"
    _qc(env, "chem-01", ["GLU"])
    _run(env, "chem-01", "S-1001", ["GLU"])
    assert env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["rule"] == "order_state"


def test_patient_mismatch_held_and_recollection_puts_order_on_hold():
    env = build({"chem-01": {"patient_mismatch": ["S-1001"]}})
    env.clinical.receive_order(SR_CHEM)
    env.clinical.invoke("dispatch_order", {"order_id": "sr-1001"})
    _qc(env, "chem-01", ["GLU", "K"])
    _run(env, "chem-01", "S-1001", ["GLU", "K"])
    rows = env.clinical.read("results:S-1001")
    assert all(r["release"] == "held" and any("patient_mismatch" in h for h in r["hold_reasons"]) for r in rows)
    assert env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["rule"] == "result_held"
    env.clinical.invoke("request_recollection", {"specimen_id": "S-1001", "reason": "possible wrong patient"})
    assert env.clinical.orders["sr-1001"].task_status == "on-hold"
    assert any(t["code"]["text"] == "recollect-specimen" for t in env.ehr.resources("Task"))


def test_qc_gate_blocks_release():
    env = build({"chem-01": {"qc_shift": {"GLU": 3.5}}})
    _qc(env, "chem-01", ["GLU"])
    assert env.clinical.read("qc_status:chem-01:GLU")["violations"] == ["1_3s"]
    _run(env, "chem-01", "S-1001", ["GLU"])
    assert env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["rule"] == "qc_gate"


def test_critical_needs_human_and_sends_communication():
    env = build({"chem-01": {"critical": ["S-1002"]}})
    _qc(env, "chem-01", ["GLU", "K"])
    _run(env, "chem-01", "S-1002", ["GLU", "K"])
    crit = next(r for r in env.clinical.read("results:S-1002") if r["release"] == "pending_ack")
    assert env.clinical.invoke("release_results", {"specimen_id": "S-1002"})["rule"] == "critical_unacknowledged"
    req = env.clinical.invoke("request_critical_callback", {"result_id": crit["result_id"], "recipient": "Dr On Call"})
    assert req["status"] == "needs_approval" and not env.ehr.resources("Communication")
    assert env.clinical.invoke("release_results", {"specimen_id": "S-1002"})["rule"] == "critical_unacknowledged"
    env.operator.decide(req["request_id"], "human:supervisor", True)
    comm = env.ehr.resources("Communication")[0]
    assert comm["about"] == [{"reference": f"Observation/{crit['result_id']}"}]
    assert "by human:supervisor" in json.dumps(comm)          # the record names the person who made the call
    assert env.clinical.invoke("release_results", {"specimen_id": "S-1002"})["status"] == "ok"


# --- bedside -----------------------------------------------------------------------------

def test_vitals_charted_only_after_nurse_validation():
    env = build()
    req = env.clinical.invoke("chart_vitals", {"device_id": "mon-12"})
    assert req["status"] == "needs_approval" and not env.ehr.bundles
    env.operator.decide(req["request_id"], "human:nurse", True)
    obs = env.ehr.resources("Observation")
    assert len(obs) == 6 and all(o["category"][0]["coding"][0]["code"] == "vital-signs" for o in obs)
    assert env.clinical.invoke("chart_vitals", {"device_id": "mon-12"})["rule"] == "already_charted"


@pytest.mark.parametrize("faults,reason", [({"artefact": ["MDC_PULS_OXIM_SAT_O2"]}, "artefact"),
                                           ({"lead_off": ["MDC_ECG_HEART_RATE"]}, "lead_off"),
                                           ({"unassociated": True}, "unassociated_device")])
def test_vitals_held_for_signal_quality_and_association(faults, reason):
    env = build({"mon-12": faults})
    r = env.clinical.invoke("chart_vitals", {"device_id": "mon-12"})
    assert r["rule"] == "vitals_held" and reason in r["reason"]


def test_denied_approval_executes_nothing():
    env = build()
    req = env.clinical.invoke("chart_vitals", {"device_id": "mon-12"})
    env.operator.decide(req["request_id"], "human:nurse", False)
    assert env.operator.status(req["request_id"])["status"] == "denied" and not env.ehr.bundles
