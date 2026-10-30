"""The knowledge ablation: device profiles without the operating knowledge a lab writes into them."""

from mhs_bridge.environment import build
from mhs_bridge.evals.agents import SYSTEM_PROMPT, system_prompt
from mhs_bridge.evals.harness import run_suite
from mhs_bridge.evals.scenarios import BY_NAME
from mhs_bridge.tools import dispatch

FOAMING = {"Plate": "P1", "Row": "A", "Stock": "RES:BSA", "TopFraction": 1.0, "Steps": 8, "FlowRate": 140,
           "MixCycles": 5}


def test_reference_files_lose_tags_and_hints_but_keep_what_the_instrument_says():
    on, off = build().runtime.describe("lh-01"), build(knowledge="off").runtime.describe("lh-01")
    assert "Operating knowledge" in on and "Operating knowledge" not in off
    assert "physical" in on.lower() and "physical" not in off.lower()
    assert "foam or bubbles" in off                      # the vendor's Feature Definition text stays
    assert "enforced range 1 to 200 uL" in off           # and so do the limits


def test_errors_and_refusals_carry_no_explanation_without_knowledge():
    env = build(knowledge="off")
    err = dispatch(env, "mhs_invoke", {"device_id": "lh-01", "procedure": "SerialDilution", "args": FOAMING})
    assert err["error"] == "FoamDetected" and "hint" not in err
    again = dispatch(env, "mhs_invoke", {"device_id": "lh-01", "procedure": "SerialDilution", "args": FOAMING})
    assert again["rule"] == "quarantine" and again["hint"] == ""      # still refused, no longer explained


def test_the_prompt_drops_only_the_physical_failure_line():
    off = system_prompt("off")
    assert system_prompt("on") == SYSTEM_PROMPT and "foam" not in off.lower()
    assert len(SYSTEM_PROMPT.splitlines()) - len(off.splitlines()) == 2


def test_scripted_results_do_not_depend_on_the_tags():
    scenarios = [BY_NAME[n] for n in ("lab_foam_recovery", "lab_viscous_first_time", "lab_saturation_rerun")]
    off = run_suite(["reference"], scenarios, ablation=False, knowledge="off")
    assert all(r.success and r.knowledge == "off" for r in off)
