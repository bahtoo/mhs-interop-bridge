"""The review queue: drafts, the state machine, approval events, copilot mode, and data exceptions."""

import json
from dataclasses import replace

import pytest

from mhs_bridge.apps.operator import ReviewError, verify_chain
from mhs_bridge.environment import build
from mhs_bridge.evals.invariants import check_invariants
from mhs_bridge.evals.scenarios import SR_CHEM, SR_NO_SPECIMEN
from mhs_bridge.tools import dispatch

TRANSFER = {"Source": "RES:DYE", "Target": "P1:A1", "Volume": 100, "FlowRate": 140, "MixCycles": 0}


def _ready(env, spec="S-1001", tests=("GLU", "K")):
    for t in tests:
        env.runtime.invoke("chem-01", "run_qc", {"test": t, "level": "L1"})
    env.runtime.invoke("chem-01", "run_specimen", {"specimen_id": spec, "tests": list(tests)})


def test_release_is_a_draft_until_a_human_approves_it():
    env = build()
    _ready(env)
    res = env.clinical.invoke("release_results", {"specimen_id": "S-1001"}, rationale="QC passed, no holds")
    assert res["status"] == "needs_approval" and not env.ehr.bundles
    d = env.operator.approvals[res["request_id"]]
    review = d.review()
    assert review["status"] == "PENDING_REVIEW" and review["payload"]["format"] == "fhir+json"
    assert review["intent"]["source"]["type"] == "paper_request" and review["intent"]["agent_rationale"]
    assert any(f["path"].endswith(".valueQuantity") and "raw" in f["from"] for f in review["proposed"]["fields"])
    out = env.operator.decide(res["request_id"], "human:tech", True, note="checked against the analyzer")
    assert out["request"] == "EXECUTED" and d.decided_by == "human:tech"
    bundle = env.ehr.bundles[-1]
    assert bundle["meta"]["tag"][0]["code"] == res["request_id"]
    prov = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Provenance")
    assert prov["signature"][0]["who"]["display"] == "human:tech" and prov["signature"][0]["data"] == d.payload_sha256
    assert [h["status"] for h in d.history] == ["PENDING_REVIEW", "APPROVED", "EXECUTED"]
    assert not check_invariants(env, False, set())


def test_illegal_transitions_raise_and_nothing_runs_without_an_approval_event():
    env = build()
    _ready(env)
    rid = env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["request_id"]
    d = env.operator.approvals[rid]
    with pytest.raises(ReviewError):
        d.move("EXECUTED", "agent")                       # PENDING_REVIEW cannot jump to EXECUTED
    with pytest.raises(ReviewError):
        env.clinical.execute_approved("release_results", d.args, "human:tech", d)   # not approved: no transport
    assert not env.ehr.bundles
    env.operator.decide(rid, "human:tech", False)
    assert env.operator.decide(rid, "human:tech", True)["status"] == "error"        # a rejection is final
    assert not env.ehr.bundles and d.status == "REJECTED"


def test_content_that_changed_after_approval_is_not_sent():
    env = build()
    _ready(env)
    rid = env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["request_id"]
    env.runtime.invoke("chem-01", "run_specimen", {"specimen_id": "S-1001", "tests": ["GLU", "K"]})   # new results
    out = env.operator.decide(rid, "human:tech", True)
    assert out["request"] == "EXECUTION_BLOCKED" and out["result"]["rule"] == "payload_changed"
    assert not env.ehr.resources("Observation")


def test_asking_twice_returns_the_same_pending_draft():
    env = build()
    _ready(env)
    a = env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["request_id"]
    b = env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["request_id"]
    assert a == b and len(env.operator.approvals) == 1


def test_a_tampered_approval_record_stops_execution():
    env = build()
    _ready(env)
    first = env.clinical.invoke("chart_vitals", {"device_id": "mon-12"})["request_id"]
    env.operator.decide(first, "human:nurse", True)
    assert verify_chain(env.operator.events) == ""
    env.operator.events[0] = replace(env.operator.events[0], approver="human:someone-else")   # rewrite history
    assert "changed after it was recorded" in verify_chain(env.operator.events)
    rid = env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["request_id"]
    out = env.operator.decide(rid, "human:tech", True)
    assert out["request"] == "EXECUTION_BLOCKED" and "do not verify" in out["result"]["reason"]
    assert not env.ehr.resources("DiagnosticReport")
    assert any(v.startswith("I13 approval events do not verify") for v in check_invariants(env, False, set()))


def test_invariant_catches_a_release_that_differs_from_what_was_approved():
    env = build()
    _ready(env)
    rid = env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["request_id"]
    env.operator.decide(rid, "human:tech", True)
    obs = next(e["resource"] for e in env.ehr.bundles[-1]["entry"] if e["resource"]["resourceType"] == "Observation")
    obs["valueQuantity"]["value"] = 1.0                       # altered after approval, on the way to the EHR
    assert any("is not what was approved" in v for v in check_invariants(env, False, set()))


def test_missing_accession_is_held_with_suggestions_and_resolved_only_by_a_human():
    env = build()
    env.clinical.receive_order(SR_NO_SPECIMEN)
    o = env.clinical.orders["sr-4001"]
    exc = o.exceptions[0]
    assert exc["issue"] == "missing_accession" and exc["suggestions"][0]["value"] == "S-1001"
    assert exc["suggestions"][0]["plausible"] and not any(s["verified"] for s in exc["suggestions"])
    assert env.clinical.invoke("dispatch_order", {"order_id": "sr-4001"})["rule"] == "data_exception"
    res = env.clinical.invoke("propose_resolution", {"order_id": "sr-4001", "exception_id": exc["exception_id"],
                                                     "value": "REQ-77", "basis": "requisition number"})
    assert res["status"] == "needs_approval" and o.specimen_id == ""                 # proposed, not applied
    rev = env.operator.revise(res["request_id"], "human:tech", {"value": "S-1001"}, note="read the tube barcode")
    old, new = env.operator.approvals[res["request_id"]], env.operator.approvals[rev["request_id"]]
    assert old.status == "SUPERSEDED" and old.superseded_by == new.request_id and new.revision == 2
    assert any(f.get("changed_in_revision") == 2 for f in new.proposed)
    assert o.specimen_id == ""
    env.operator.decide(new.request_id, "human:tech", True)
    assert o.specimen_id == "S-1001" and not o.exceptions
    assert "human:tech" in o.history[-1]["reason"]
    draft = env.operator.approvals[env.clinical.invoke("dispatch_order", {"order_id": "sr-4001"})["request_id"]]
    assert any(f["path"].endswith(".specimen_id") and "approved by human:tech" in f["from"] for f in draft.proposed)


def test_unknown_unit_stays_held_and_lists_ranked_candidates():
    env = build({"hema-01": {"unit_drift": {"HGB": {"value": 8.7, "unit": "mmol/L"}}}})
    for t in ("WBC", "HGB"):
        env.runtime.invoke("hema-01", "run_qc", {"test": t, "level": "L1"})
    env.runtime.invoke("hema-01", "run_specimen", {"specimen_id": "H-2001", "tests": ["WBC", "HGB", "PLT"]})
    hgb = next(r for r in env.clinical.read("results:H-2001") if r["test_code"] == "718-7")
    assert hgb["release"] == "held" and hgb["value"] is None
    best = hgb["exceptions"][0]["suggestions"][0]
    assert best["value"] == {"value": 14.0, "unit": "g/dL"} and best["plausible"] and not best["verified"]
    assert env.clinical.invoke("release_results", {"specimen_id": "H-2001"})["rule"] == "result_held"


def test_copilot_drafts_device_invokes_but_the_emergency_stop_is_immediate():
    env = build(mode="copilot")
    res = dispatch(env, "mhs_invoke", {"device_id": "lh-01", "procedure": "Transfer", "args": TRANSFER,
                                       "rationale": "dye into A1"})
    assert res["status"] == "needs_approval" and not env.world.transfers
    over = dispatch(env, "mhs_invoke", {"device_id": "lh-01", "procedure": "Transfer",
                                        "args": {**TRANSFER, "Volume": 250}})
    assert over["status"] == "blocked" and over["rule"] == "limit"                  # limits apply before drafting
    assert dispatch(env, "mhs_emergency_stop", {"reason": "spill"})["estop"] is True    # never waits for review
    out = env.operator.decide(res["request_id"], "human:op", True)
    assert out["request"] == "EXECUTION_BLOCKED" and out["result"]["rule"] == "emergency_stop"
    assert not env.world.transfers


def test_copilot_approval_runs_the_invoke_under_the_approver():
    env = build(mode="copilot")
    rid = dispatch(env, "mhs_invoke", {"device_id": "lh-01", "procedure": "Transfer", "args": TRANSFER})["request_id"]
    assert env.operator.decide(rid, "human:op", True)["request"] == "EXECUTED" and len(env.world.transfers) == 1
    run = next(e for e in env.runtime.audit if e.kind == "invoke" and e.outcome == "executed")
    assert run.actor == "human:op" and not check_invariants(env, False, set())


def test_file_channel_keeps_an_append_only_review_log_and_refuses_after_tampering(tmp_path, capsys):
    from mhs_bridge.cli import cmd_show
    from mhs_bridge.mcp_server import FileOperatorChannel

    env = build()
    env.clinical.receive_order(SR_CHEM)
    ch = FileOperatorChannel(env, tmp_path)
    rid = env.clinical.invoke("dispatch_order", {"order_id": "sr-1001"})["request_id"]
    ch.sync()
    assert cmd_show(str(tmp_path), rid) == 0
    shown = capsys.readouterr().out
    assert "INTENT  ServiceRequest sr-1001" in shown and "PROPOSED MAPPING" in shown and "ASTM E1394" in shown
    (tmp_path / "decisions.jsonl").write_text(json.dumps({"request_id": rid, "approve": True,
                                                          "approver": "human:tech"}) + "\n")
    ch.sync()
    log = (tmp_path / "review.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["decision"] == "approve"
    assert json.loads((tmp_path / "review_status.json").read_text())["chain"] == "ok"
    assert any(a["subtype"][0]["code"] == "EXECUTED" for a in json.loads((tmp_path / "audit_events.json").read_text()))

    (tmp_path / "review.jsonl").write_text(log[0].replace("human:tech", "human:mallory") + "\n")
    _ready(env)
    rel = env.clinical.invoke("release_results", {"specimen_id": "S-1001"})["request_id"]
    with (tmp_path / "decisions.jsonl").open("a") as fh:
        fh.write(json.dumps({"request_id": rel, "approve": True, "approver": "human:tech"}) + "\n")
    ch.sync()
    assert json.loads((tmp_path / "review_status.json").read_text())["chain"] == "broken"
    assert env.operator.approvals[rel].status == "PENDING_REVIEW" and not env.ehr.resources("DiagnosticReport")
