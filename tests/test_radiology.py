"""Radiology device operations: a CT's daily QA, air calibration, and the injector that is read only."""

import base64

from mhs_bridge.environment import build
from mhs_bridge.evals.harness import run_one
from mhs_bridge.evals.scenarios import BY_NAME
from mhs_bridge.sim.imaging import _unpng

CT = "ct-01"


def _do(env, proc):
    return env.runtime.invoke(CT, proc, {})


def _by_hand(env, task):
    req = env.service.invoke("request_work", {"device_id": CT, "task": task, "reason": "daily QA"})
    env.operator.decide(req["request_id"], "human:radiographer", True)


def test_start_of_day_needs_a_warm_tube_and_the_phantom_on_the_table():
    env = build({CT: {"start_of_day": True}})
    assert env.runtime.read(CT, "clinical_use")["released"] is False
    assert _do(env, "scan_qa_phantom")["rule"] == "tube_cold"
    _do(env, "tube_warmup")
    assert _do(env, "scan_qa_phantom")["rule"] == "no_phantom"
    _by_hand(env, "place_water_phantom")
    assert _do(env, "scan_qa_phantom")["qa"]["pass"]
    assert _do(env, "release_for_clinical_use")["rule"] == "gantry_not_clear"     # phantom still on the table
    _by_hand(env, "remove_water_phantom")
    assert _do(env, "release_for_clinical_use")["status"] == "ok"


def test_a_stale_air_calibration_fails_uniformity_and_needs_an_empty_gantry_to_fix():
    env = build({CT: {"start_of_day": True, "air_cal_stale": True}})
    _do(env, "tube_warmup")
    _by_hand(env, "place_water_phantom")
    qa = _do(env, "scan_qa_phantom")["qa"]
    assert not qa["pass"] and not qa["checks"]["uniformity_hu"]
    assert _do(env, "air_calibration")["rule"] == "gantry_not_clear"
    _by_hand(env, "remove_water_phantom")
    _do(env, "air_calibration")
    assert env.runtime.read(CT, "qa_status")["passing"] is False                # QA has to be repeated
    _by_hand(env, "place_water_phantom")
    assert _do(env, "scan_qa_phantom")["qa"]["pass"]


def test_an_air_calibration_with_the_phantom_in_calibrates_the_phantom_in():
    env = build({CT: {"start_of_day": True}})
    env.runtime.drivers[CT].instrument.tube_warm = True
    _by_hand(env, "place_water_phantom")
    env.runtime.drivers[CT].write("air_calibration", {})                         # past the interlock
    qa = _do(env, "scan_qa_phantom")["qa"]
    assert qa["mean_hu"] < -10 and not qa["pass"]


def test_the_qa_image_shows_the_phantom():
    env = build({CT: {"start_of_day": True, "detector_fault": True}})
    _do(env, "tube_warmup")
    _by_hand(env, "place_water_phantom")
    _do(env, "scan_qa_phantom")
    img = env.runtime.read(CT, "qa_image")
    px = _unpng(base64.b64decode(img["data_base64"]))
    assert img["media_type"] == "image/png" and px[0][0] == (0, 0, 0) and px[48][48][0] > 100
    assert max(px[48][48 + 18]) > max(px[48][48 + 10]) + 30                     # the ring


def test_a_detector_fault_survives_air_calibration_and_logs_the_part():
    env = build({CT: {"start_of_day": True, "detector_fault": True}})
    _do(env, "tube_warmup")
    _do(env, "air_calibration")
    assert env.runtime.read(CT, "event_log")[-1]["code"] == "D-412"
    case = env.service.invoke("open_case", {"device_id": CT, "summary": "ring after air cal"})["case_id"]
    req = env.service.invoke("dispatch_engineer", {"case_id": case, "part": "DMS-3 detector module",
                                                   "evidence": "D-412"})
    assert req["status"] == "needs_approval"
    assert env.service.invoke("close_case", {"case_id": case, "resolution": "x"})["rule"] == "qc_not_passing"


def test_the_injector_is_read_only():
    env = build()
    assert env.runtime.read("inj-01", "injector")["armed"] is False
    assert env.runtime.invoke("inj-01", "arm", {})["rule"] == "read_only_device"


def test_the_radiology_scenarios():
    for name in ("rad_daily_qa", "rad_detector_fault"):
        sc = BY_NAME[name]
        assert run_one(sc, "reference").success, name
        naive = run_one(sc, "naive")
        assert not naive.success and not naive.violations, name
        off = run_one(sc, "naive", guardrails=False).violations
        assert {v[:2] for v in off} >= {"R1", "R2", "R3"}, name
