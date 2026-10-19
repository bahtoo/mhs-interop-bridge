"""Drivers: the open-protocol adapters that are this repository's contribution."""

from mhs_bridge.canonical import ReleaseState
from mhs_bridge.environment import build


# --- SiLA 2 -----------------------------------------------------------------------

def test_sila2_driver_generates_manifest_from_feature_definition():
    m = build().runtime.manifest("lh-01")
    transfer = m.procedure("Transfer")
    vol = next(p for p in transfer.params if p.name == "Volume")
    assert (vol.min, vol.max, vol.unit) == (1.0, 200.0, "uL")
    assert "FoamDetected" in transfer.errors and "Move to a clean well" in transfer.errors["FoamDetected"]
    assert "SiLA 2 org.example.mhsbridge/liquidhandling/LiquidHandlingService/v1" in transfer.description
    assert m.procedure("SerialDilution").routine
    assert {s.name for s in m.states} == {"status", "PlateMap", "DeckLayout"}


def test_foam_quarantine_blocks_same_well_retry():
    rt = build().runtime
    first = rt.invoke("lh-01", "Transfer", {"Source": "RES:BSA", "Target": "P1:A1", "Volume": 150, "FlowRate": 140,
                                            "MixCycles": 5})
    assert first["error"] == "FoamDetected" and first["Well"] == "P1:A1" and "hint" in first
    retry = rt.invoke("lh-01", "Transfer", {"Source": "RES:BSA", "Target": "P1:A1", "Volume": 10, "FlowRate": 10,
                                            "MixCycles": 0})
    assert retry["rule"] == "quarantine" and "physical" in retry["hint"]
    clean = rt.invoke("lh-01", "Transfer", {"Source": "RES:BSA", "Target": "P1:A2", "Volume": 150, "FlowRate": 10,
                                            "MixCycles": 2})
    assert clean["status"] == "ok"


def test_capacity_and_deck_interlocks():
    env = build()
    rt = env.runtime
    buf = {"Source": "RES:BUF", "FlowRate": 140, "MixCycles": 0}
    for _ in range(2):
        assert rt.invoke("lh-01", "Transfer", {**buf, "Target": "P1:A1", "Volume": 150})["status"] == "ok"
    assert rt.invoke("lh-01", "Transfer", {**buf, "Target": "P1:A1", "Volume": 10})["rule"] == "capacity"
    rt.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "reader-01"})
    assert rt.invoke("lh-01", "Transfer", {**buf, "Target": "P1:B1", "Volume": 10})["rule"] == "plate_location"


def test_sila_parameters_are_mandatory():
    rt = build().runtime
    r = rt.invoke("lh-01", "Transfer", {"Source": "RES:BUF", "Target": "P1:A1", "Volume": 10, "FlowRate": 10})
    assert r["rule"] == "missing_param" and "MixCycles" in r["reason"]


def test_plate_mover_refuses_unreachable_destination():
    env = build({"reader-01": {"offline": True}})
    r = env.runtime.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "reader-01"})
    assert r["error"] == "DestinationUnavailable" and env.world.plates["P1"].location == "lh-01"


# --- file drop ----------------------------------------------------------------------

def test_file_drop_reader_round_trip(tmp_path):
    env = build(folder=tmp_path)
    rt = env.runtime
    assert rt.invoke("reader-01", "ReadPlate", {"Plate": "P1", "WavelengthNm": 562})["rule"] == "plate_location"
    rt.invoke("lh-01", "Transfer", {"Source": "RES:DYE", "Target": "P1:A1", "Volume": 100, "FlowRate": 140,
                                    "MixCycles": 0})
    rt.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "reader-01"})
    r = rt.invoke("reader-01", "ReadPlate", {"Plate": "P1", "WavelengthNm": 562})
    assert r["status"] == "ok" and (tmp_path / "reader-01" / "export" / r["export"]).exists()
    reading = rt.read("reader-01", "last_read:P1")
    assert reading["A1"] == 3.0           # undiluted 200 ug/mL dye saturates the detector
    assert reading["B1"] == 0.0           # empty well


# --- clinical analyzers -------------------------------------------------------------

def _run(env, dev, spec, tests):
    return env.runtime.invoke(dev, "run_specimen", {"specimen_id": spec, "tests": tests})


def test_astm_analyzer_normalises_to_loinc_and_ucum():
    env = build()
    _run(env, "chem-01", "S-1001", ["GLU", "K"])
    glu = env.runtime.read("chem-01", "results:S-1001")[0]
    assert (glu["test_code"], glu["value"], glu["unit"], glu["interpretation"]) == ("2345-7", 94.0, "mg/dL", "N")


def test_astm_line_noise_recovered_and_nak_storm_aborts():
    env = build({"chem-01": {"line_noise_naks": 2}})
    assert _run(env, "chem-01", "S-1001", ["GLU"])["status"] == "ok"
    assert env.runtime.read("chem-01", "connection")["retransmissions"] > 0
    env = build({"chem-01": {"nak_storm": True}})
    assert _run(env, "chem-01", "S-1001", ["GLU"])["error"] == "ASTM_SESSION_ABORTED"


def test_hl7_analyzer_converts_units_and_holds_unknown_unit():
    env = build()
    _run(env, "hema-01", "H-2001", ["HGB"])
    assert env.runtime.read("hema-01", "results:H-2001")[0]["value"] == 14.0
    env = build({"hema-01": {"unit_drift": {"HGB": {"value": 8.7, "unit": "mmol/L"}}}})
    _run(env, "hema-01", "H-2001", ["HGB"])
    r = env.runtime.read("hema-01", "results:H-2001")[0]
    assert r["value"] is None and r["interpretation"] == "" and "unit_mismatch" in r["hold_reasons"][0]


def test_specimen_quarantine_interlock():
    env = build({"chem-01": {"foam": ["S-1001"]}})
    _run(env, "chem-01", "S-1001", ["GLU"])
    assert env.runtime.read("chem-01", "results:S-1001")[0]["conditions"] == ["foam"]
    assert _run(env, "chem-01", "S-1001", ["GLU"])["rule"] == "specimen_quarantine"


def test_order_download_over_astm_and_hl7():
    env = build()
    args = {"specimen_id": "S-9", "patient_id": "P-9", "tests": ["GLU"], "order_id": "sr-9", "priority": "stat"}
    assert env.runtime.invoke("chem-01", "download_order", args)["message"] == "ASTM host order"
    assert env.runtime.read("chem-01", "worklist")["S-9"]["priority"] == "S"
    hl7 = env.runtime.invoke("hema-01", "download_order", {**args, "tests": ["WBC"]})
    assert hl7["message"] == "OML^O33"


def test_monitor_maps_mdc_to_loinc():
    env = build()
    v = {r["test_code"]: (r["value"], r["unit"]) for r in env.runtime.read("mon-12", "latest_vitals")["results"]}
    assert v["8867-4"] == (78.0, "/min") and v["59408-5"] == (97.0, "%") and v["8480-6"] == (122.0, "mm[Hg]")
    assert ReleaseState.RELEASABLE.value == "releasable"
