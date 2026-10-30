"""Visual evidence: a plate photo shows foam that no instrument reports."""

import asyncio
import base64
import hashlib

import pytest

from mhs_bridge.environment import build
from mhs_bridge.evals.agents import SimulatedHuman, Toolbox
from mhs_bridge.evals.harness import run_one
from mhs_bridge.evals.scenarios import BY_NAME
from mhs_bridge.sim.imaging import _unpng, foamed_wells, render_plate
from mhs_bridge.tools import dispatch, render_blocks

FOAMING = {"Plate": "P1", "Row": "A", "Stock": "RES:BSA", "TopFraction": 1.0, "Steps": 8, "FlowRate": 140,
           "MixCycles": 5}


def _foam_then_read(env):
    rt = env.runtime
    res = rt.invoke("lh-01", "SerialDilution", FOAMING)
    rt.invoke("arm-01", "MovePlate", {"Plate": "P1", "Destination": "reader-01"})
    rt.invoke("reader-01", "ReadPlate", {"Plate": "P1", "WavelengthNm": 562})
    return res


def test_with_detection_off_foam_is_unreported_but_visible():
    env = build({"lh-01": {"lld_off": True}})
    assert _foam_then_read(env)["status"] == "ok"                       # no FoamDetected
    assert not any(w["foam"] for w in env.runtime.read("lh-01", "PlateMap")["P1"].values())
    truth = sorted(w for _, w in env.world.foam_events)
    photo = env.runtime.read("reader-01", "last_image:P1")
    assert photo["media_type"] == "image/png"
    assert foamed_wells(base64.b64decode(photo["data_base64"])) == [w.split(":")[1] for w in truth]


def test_the_image_is_deterministic_and_shows_nothing_on_a_clean_plate():
    env = build()
    pixels = hashlib.sha256(repr(_unpng(render_plate(env.world.plates["P1"]))).encode()).hexdigest()
    assert pixels == hashlib.sha256(repr(_unpng(render_plate(build().world.plates["P1"]))).encode()).hexdigest()
    assert foamed_wells(render_plate(env.world.plates["P1"])) == []


def test_a_model_receives_the_photo_as_an_image_and_transcripts_keep_no_pixels():
    env = build({"lh-01": {"lld_off": True}})
    _foam_then_read(env)
    photo = dispatch(env, "mhs_read", {"device_id": "reader-01", "state": "last_image:P1"})
    blocks = render_blocks(env, photo)
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert "data_base64" not in blocks[1]["text"]
    tb = Toolbox(env, SimulatedHuman())
    tb.read("reader-01", "last_image:P1")
    assert "data_base64" not in str(tb.transcript[-1]["output"]) and tb.transcript[-1]["output"]["image"]


def test_only_the_photo_reveals_the_foam_in_the_scenario():
    sc = BY_NAME["lab_foam_visual_only"]
    assert run_one(sc, "reference").success                             # it looks, and corrects the flow rate
    assert not run_one(sc, "naive").success
    off = run_one(sc, "naive", guardrails=False)
    assert any(v.startswith("I8") for v in off.violations)              # ground truth catches the foamed row
    assert not any(v.startswith("I5") for v in run_one(sc, "reference").violations)


def test_mcp_returns_the_photo_as_image_content(tmp_path):
    pytest.importorskip("mcp")
    from mhs_bridge.mcp_server import build_server
    server, env = build_server("lab_foam_visual_only", state_dir=tmp_path)
    _foam_then_read(env)
    out = asyncio.run(server.call_tool("mhs_read", {"device_id": "reader-01", "state": "last_image:P1"}))
    blocks = out[0] if isinstance(out, tuple) else out
    assert any(getattr(b, "type", "") == "image" for b in blocks)
