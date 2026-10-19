"""Clinical vendor dialects: load profiles, normalise vendor messages to canonical results.

The codec layer (protocols/) knows the standards. The profile layer knows each
vendor's dialect: which component holds the test code, what unit it sends, what
its private flags mean. Keeping the two apart is what makes a new vendor a data
change rather than a code change.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from ..canonical import CanonicalResult, ReleaseState
from ..protocols import astm, hl7

_ids = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f"{prefix}-{next(_ids):05d}"


@dataclass
class Profile:
    data: dict

    @property
    def device_id(self) -> str:
        return self.data["device_id"]

    @property
    def protocol(self) -> str:
        return self.data["protocol"]

    @property
    def device_type(self) -> str:
        return self.data["device_type"]

    @property
    def tests(self) -> dict:
        return self.data.get("tests", {})

    @property
    def tags(self) -> list[str]:
        return self.data.get("tags", [])

    @property
    def limits(self) -> dict:
        return self.data.get("limits", {})


def load_profile(path: str | Path) -> Profile:
    with open(path, encoding="utf-8") as fh:
        return Profile(yaml.safe_load(fh))


def builtin_profile_data() -> dict[str, dict]:
    """Every device profile shipped with the package, keyed by device id."""
    folder = resources.files("mhs_bridge") / "profiles"
    out: dict[str, dict] = {}
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".yaml"):
            data = yaml.safe_load(entry.read_text(encoding="utf-8"))
            out[data["device_id"]] = data
    order = {"research_automation": 0, "specimen_processing": 1, "patient_connected": 2}
    return dict(sorted(out.items(), key=lambda kv: (order.get(kv[1].get("risk_class", ""), 3), kv[0])))


def builtin_profiles() -> dict[str, Profile]:
    """Clinical dialect profiles (devices whose driver decodes ASTM or HL7 results)."""
    return {k: Profile(v) for k, v in builtin_profile_data().items() if v.get("protocol") in ("astm", "hl7")}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _convert(profile: Profile, code: str, raw_value: str, raw_unit: str
             ) -> tuple[float | None, str, list[str]]:
    """Return (value, unit, hold_reasons) after unit normalisation.

    A unit that is neither the vendor's declared unit nor the target unit is a
    hold, never a guess: silently mis-scaled results are a patient safety issue.
    """
    spec = profile.tests[code]
    try:
        value = float(raw_value)
    except ValueError:
        return None, spec["unit"], [f"non-numeric value {raw_value!r}"]
    decimals = int(spec.get("decimals", 2))
    if raw_unit == spec["unit"]:
        return round(value, decimals), spec["unit"], []
    if raw_unit == spec["vendor_unit"]:
        return round(value * float(spec["factor"]), decimals), spec["unit"], []
    return None, spec["unit"], [
        f"unit_mismatch: received {raw_unit!r}, profile expects {spec['vendor_unit']!r}"]


def _interpret(profile: Profile, code: str, value: float | None, vendor_flags: list[str]) -> str:
    spec = profile.tests[code]
    if value is not None:
        if "critical_low" in spec and value < spec["critical_low"]:
            return "LL"
        if "critical_high" in spec and value > spec["critical_high"]:
            return "HH"
        if spec.get("ref_low") is not None and value < spec["ref_low"]:
            return "L"
        if spec.get("ref_high") is not None and value > spec["ref_high"]:
            return "H"
        return "N"
    # No numeric value (held or non-numeric): never claim "normal". Only a
    # directional vendor flag such as ">" can still say something useful.
    mapping = profile.data.get("abnormal_flags", {})
    for f in vendor_flags:
        if f in mapping and mapping[f] != "N":
            return mapping[f]
    return ""


def _build(profile: Profile, *, code: str, raw_value: str, raw_unit: str, flags: list[str],
           specimen: str, patient: str, status: str, observed_at: str, raw: dict
           ) -> CanonicalResult:
    hold: list[str] = []
    if code not in profile.tests:
        spec = {"loinc": "", "display": code, "unit": raw_unit}
        value, unit = None, raw_unit
        hold.append(f"unmapped_test: vendor code {code!r} has no LOINC mapping in profile")
    else:
        spec = profile.tests[code]
        value, unit, reasons = _convert(profile, code, raw_value, raw_unit)
        hold.extend(reasons)
    cond_map = profile.data.get("condition_flags", {})
    conditions = sorted({cond_map[f] for f in flags if f in cond_map})
    hold.extend(f"instrument_condition: {c}" for c in conditions)
    interp = _interpret(profile, code, value, flags) if code in profile.tests else ""
    return CanonicalResult(
        result_id=_next_id("res"),
        device_id=profile.device_id,
        specimen_id=specimen,
        patient_id=patient,
        test_code=spec["loinc"],
        test_display=spec["display"],
        value=value,
        unit=unit,
        ref_low=spec.get("ref_low"),
        ref_high=spec.get("ref_high"),
        interpretation=interp,
        status=status,
        observed_at=observed_at,
        conditions=conditions,
        vendor_raw={"code": code, "value": raw_value, "unit": raw_unit, "flags": flags, **raw},
        release=ReleaseState.HELD if hold else ReleaseState.RELEASABLE,
        hold_reasons=hold,
    )


_ASTM_STATUS = {"F": "final", "P": "preliminary", "C": "corrected"}
_HL7_STATUS = {"F": "final", "P": "preliminary", "C": "corrected"}


def normalise_astm(profile: Profile, message: str) -> list[CanonicalResult]:
    delims, records = astm.parse_message(message)
    cfg = profile.data.get("astm", {})
    comp = int(cfg.get("test_id_component", 4))
    spec_field = int(cfg.get("specimen_field", 3))
    pat_field = int(cfg.get("patient_field", 3))
    out: list[CanonicalResult] = []
    specimen = patient = ""
    for rec in records:
        if rec.type == "P":
            patient = rec.get(pat_field)
        elif rec.type == "O":
            specimen = rec.component(spec_field, 1, delims) or rec.get(spec_field)
        elif rec.type == "R":
            code = rec.component(3, comp, delims)
            flags = [f for f in rec.get(7).split(delims.repeat) if f]
            out.append(_build(
                profile, code=code, raw_value=rec.get(4), raw_unit=rec.get(5), flags=flags,
                specimen=specimen, patient=patient,
                status=_ASTM_STATUS.get(rec.get(9), "preliminary"),
                observed_at=rec.get(13), raw={"record": delims.field.join(rec.fields)}))
    return out


def normalise_hl7(profile: Profile, message: str) -> list[CanonicalResult]:
    msg = hl7.parse(message)
    allowed = profile.data.get("hl7", {}).get("result_messages", ["OUL^R22", "ORU^R01"])
    if msg.message_type not in allowed:
        raise hl7.HL7Error(f"{msg.message_type} not accepted by profile {profile.device_id}")
    pid = msg.first("PID")
    patient = pid.component(3, 1) if pid else ""
    out: list[CanonicalResult] = []
    for obs in hl7.extract_observations(msg):
        out.append(_build(
            profile, code=obs.vendor_code, raw_value=obs.value, raw_unit=obs.units,
            flags=obs.abnormal_flags, specimen=obs.specimen_id, patient=patient,
            status=_HL7_STATUS.get(obs.status, "preliminary"), observed_at=obs.observed_at,
            raw={"notes": obs.notes}))
    return out


def normalise(profile: Profile, message: str) -> list[CanonicalResult]:
    if profile.protocol == "astm":
        return normalise_astm(profile, message)
    if profile.protocol == "hl7":
        return normalise_hl7(profile, message)
    raise ValueError(f"profile {profile.device_id} protocol {profile.protocol!r} has no result codec")
