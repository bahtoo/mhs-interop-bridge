import pytest

from mhs_bridge.canonical import CanonicalResult
from mhs_bridge.protocols import astm, fhir, hl7


# --- ASTM E1381 -------------------------------------------------------------

def test_checksum_hand_computed():
    # '1' 'A' 'B' 'C' ETX = 0x31+0x41+0x42+0x43+0x03 = 250 = 0xFA
    assert astm.checksum(b"1ABC\x03") == b"FA"


def test_frame_round_trip_and_numbering():
    msg = astm.build_message([["H", "", "", "SENDER"], ["P", "1", "PAT"], ["L", "1", "N"]])
    frames = astm.build_frames(msg)
    assert [f[1:2] for f in frames] == [b"1", b"2", b"3"]
    assert all(f.startswith(astm.STX) and f.endswith(b"\r\n") for f in frames)
    assert astm.reassemble(frames) == msg


def test_long_record_splits_with_etb():
    long_comment = "C|1|I|" + "X" * 600 + "|G"
    msg = astm.build_message([["H"], long_comment.split("|"), ["L", "1"]])
    frames = astm.build_frames(msg)
    record_frames = frames[1:-1]
    assert len(record_frames) == 3
    assert all(f[-5:-4] == astm.ETB for f in record_frames[:-1])
    assert record_frames[-1][-5:-4] == astm.ETX
    assert astm.reassemble(frames) == msg


def test_frame_number_wraps_after_7():
    msg = astm.build_message([["H"]] + [["C", str(i), "I", "x"] for i in range(9)] + [["L", "1"]])
    nums = [int(f[1:2]) for f in astm.build_frames(msg)]
    assert nums[:9] == [1, 2, 3, 4, 5, 6, 7, 0, 1]


def test_corrupted_frame_rejected():
    frame = astm.build_frames(astm.build_message([["H"], ["L", "1"]]))[0]
    bad = frame[:3] + bytes([frame[3] ^ 1]) + frame[4:]
    with pytest.raises(astm.ASTMError, match="checksum"):
        astm.parse_frame(bad)


def _channel(corrupt_first: int):
    seen: dict[bytes, int] = {}

    def channel(data: bytes) -> bytes:
        if data in (astm.ENQ, astm.EOT):
            return astm.ACK
        seen[data] = seen.get(data, 0) + 1
        return astm.NAK if seen[data] <= corrupt_first else astm.ACK
    return channel


def test_session_recovers_from_naks():
    msg = astm.build_message([["H"], ["P", "1"], ["L", "1"]])
    res = astm.transfer(msg, _channel(corrupt_first=2))
    assert not res.aborted and res.message == msg
    assert res.retransmissions == 6   # 3 frames x 2 NAKs


def test_session_aborts_after_six_naks():
    msg = astm.build_message([["H"], ["L", "1"]])
    res = astm.transfer(msg, _channel(corrupt_first=99))
    assert res.aborted and res.message is None
    assert res.retransmissions == astm.MAX_RETRIES


def test_parse_message_keeps_header_delimiters():
    msg = astm.build_message([["H", "", "", "ChemPro^chem-01"], ["L", "1", "N"]])
    delims, recs = astm.parse_message(msg)
    assert (delims.field, delims.repeat, delims.component, delims.escape) == ("|", "\\", "^", "&")
    assert recs[0].get(5) == "ChemPro^chem-01"


def test_message_without_terminator_rejected():
    with pytest.raises(astm.ASTMError):
        astm.parse_message("H|\\^&|||X\rP|1\r")


# --- HL7 v2 -----------------------------------------------------------------

OUL = ("MSH|^~\\&|H5|LAB|LIS|LAB|20261019101500||OUL^R22^OUL_R22|MSG1|P|2.5.1\r"
       "PID|1||P-9^^^HOSP^MR\r"
       "SPM|1|SP-1^LAB||BLD\r"
       "OBR|1||SP-1|CBC\r"
       "OBX|1|NM|HGB^HGB^L||140|g/L^g/L^UCUM||N~CLT|||F|||20261019101500\r"
       "NTE|1||clot suspected\r")


def test_hl7_field_positions_and_type():
    msg = hl7.parse(OUL)
    msh = msg.first("MSH")
    assert msh.get(1) == "|" and msh.get(2) == "^~\\&" and msh.get(10) == "MSG1"
    assert msg.message_type == "OUL^R22"


def test_hl7_observations_with_flags_and_notes():
    obs = hl7.extract_observations(hl7.parse(OUL))
    assert len(obs) == 1
    o = obs[0]
    assert (o.specimen_id, o.vendor_code, o.value, o.units) == ("SP-1", "HGB", "140", "g/L")
    assert o.abnormal_flags == ["N", "CLT"] and o.notes == ["clot suspected"]


def test_mllp_round_trip_and_ack():
    assert hl7.mllp_unwrap(hl7.mllp_wrap(OUL)) == OUL
    with pytest.raises(hl7.HL7Error):
        hl7.mllp_unwrap(OUL.encode())
    ack = hl7.build_ack(hl7.parse(OUL), "AA")
    assert ack.first("MSA").get(1) == "AA" and ack.first("MSA").get(2) == "MSG1"
    assert ack.first("MSH").get(3) == "LIS"   # sender and receiver swap


# --- FHIR -------------------------------------------------------------------

def _result(**kw) -> CanonicalResult:
    base = dict(result_id="r1", device_id="chem-01", specimen_id="S-1", test_code="2345-7",
                test_display="Glucose", value=95.0, unit="mg/dL", ref_low=70, ref_high=99,
                interpretation="N", status="final", observed_at="20261019101500", patient_id="P-1")
    base.update(kw)
    return CanonicalResult(**base)


def test_fhir_datetime():
    assert fhir.fhir_datetime("20261019101500") == "2026-10-19T10:15:00+00:00"
    assert fhir.fhir_datetime("20261019") == "2026-10-19"


def test_fhir_observation_shape():
    o = fhir.observation(_result())
    assert o["resourceType"] == "Observation" and o["status"] == "final"
    assert o["code"]["coding"][0] == {"system": fhir.LOINC, "code": "2345-7", "display": "Glucose"}
    assert o["valueQuantity"]["system"] == fhir.UCUM and o["valueQuantity"]["code"] == "mg/dL"
    assert o["referenceRange"][0]["low"]["value"] == 70
    assert o["interpretation"][0]["coding"][0]["code"] == "N"


def test_fhir_bundle():
    b = fhir.diagnostic_report("rpt-1", [_result(), _result(result_id="r2", status="preliminary")])
    assert b["type"] == "transaction"
    report = b["entry"][-1]["resource"]
    assert report["resourceType"] == "DiagnosticReport" and report["status"] == "preliminary"
    assert [r["reference"] for r in report["result"]] == ["Observation/r1", "Observation/r2"]


# --- LIS orders ---------------------------------------------------------------

from mhs_bridge.protocols import lis_orders  # noqa: E402


def test_lis_order_round_trips():
    assert lis_orders.decode_astm_host_order(lis_orders.astm_host_order("S-9", "P-9", ["GLU", "K"], "stat")) == {
        "specimen_id": "S-9", "patient_id": "P-9", "tests": ["GLU", "K"], "priority": "S"}
    dec = lis_orders.decode_oml_o33(lis_orders.hl7_oml_o33("S-9", "P-9", ["WBC", "HGB"], "sr-9", "C1"))
    assert (dec["specimen_id"], dec["patient_id"], dec["tests"]) == ("S-9", "P-9", ["WBC", "HGB"])


# --- SiLA 2 Feature Definitions ---------------------------------------------------

from mhs_bridge.drivers.sila2 import load_builtin_feature  # noqa: E402
from mhs_bridge.protocols.sila2 import parse_feature  # noqa: E402


def test_sila2_feature_definition_parsing():
    f = load_builtin_feature("LiquidHandlingService-v1_0.sila.xml")
    assert f.fully_qualified == "org.example.mhsbridge/liquidhandling/LiquidHandlingService/v1"
    cmd = f.commands[0]
    params = {p.identifier: p.data_type for p in cmd.parameters}
    assert (params["Volume"].minimum, params["Volume"].maximum, params["Volume"].unit) == (1.0, 200.0, "uL")
    assert (params["MixCycles"].basic, params["MixCycles"].maximum) == ("Integer", 10.0)
    assert cmd.errors == ["FoamDetected", "InsufficientVolume", "PositionNotFound"]
    assert "FoamDetected" in f.errors and [p.identifier for p in f.properties] == ["PlateMap", "DeckLayout"]
    mover = load_builtin_feature("PlateHandlingService-v1_0.sila.xml")
    dest = next(p for p in mover.commands[0].parameters if p.identifier == "Destination")
    assert dest.data_type.allowed == ["lh-01", "reader-01"]


def test_sila2_rejects_non_feature_xml():
    with pytest.raises(ValueError):
        parse_feature("<NotAFeature xmlns='http://www.sila-standard.org'/>")
