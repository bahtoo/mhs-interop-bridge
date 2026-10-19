"""HL7 v2.x parsing, building and MLLP framing.

Covers what a laboratory device interface needs: MSH, PID, SPM, OBR, OBX, NTE,
the result messages ORU^R01 and OUL^R22 (IHE Laboratory Analytical Workflow),
and ACK generation. Implemented from public HL7 v2.5.1 and IHE LAW material.

MLLP envelope: <VT> message <FS><CR>
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

VT, FS, CR = "\x0b", "\x1c", "\r"


class HL7Error(Exception):
    pass


def mllp_wrap(message: str) -> bytes:
    return (VT + message + FS + CR).encode("utf-8")


def mllp_unwrap(data: bytes) -> str:
    text = data.decode("utf-8")
    if not (text.startswith(VT) and text.endswith(FS + CR)):
        raise HL7Error("invalid MLLP envelope")
    return text[1:-2]


@dataclass
class Segment:
    name: str
    fields: list[str]

    def get(self, position: int, default: str = "") -> str:
        """HL7 field by its standard position (MSH-9 is position 9)."""
        if self.name == "MSH":
            # MSH-1 is the field separator itself, so positions shift by one.
            if position == 1:
                return "|"
            idx = position - 1
        else:
            idx = position
        return self.fields[idx] if 0 <= idx < len(self.fields) else default

    def component(self, position: int, comp: int, sep: str = "^") -> str:
        parts = self.get(position).split(sep)
        return parts[comp - 1] if 0 < comp <= len(parts) else ""


@dataclass
class Message:
    segments: list[Segment] = field(default_factory=list)

    def first(self, name: str) -> Segment | None:
        return next((s for s in self.segments if s.name == name), None)

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]

    @property
    def message_type(self) -> str:
        msh = self.first("MSH")
        if msh is None:
            raise HL7Error("missing MSH")
        return msh.component(9, 1) + "^" + msh.component(9, 2)

    @property
    def control_id(self) -> str:
        msh = self.first("MSH")
        return msh.get(10) if msh else ""

    def encode(self) -> str:
        return CR.join("|".join(s.fields) for s in self.segments) + CR


def parse(message: str) -> Message:
    lines = [ln for ln in message.replace("\n", "\r").split("\r") if ln.strip()]
    if not lines or not lines[0].startswith("MSH"):
        raise HL7Error("message must start with MSH")
    if lines[0][3] != "|":
        raise HL7Error("only '|' field separator is supported")
    return Message([Segment(ln[:3], ln.split("|")) for ln in lines])


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def build_ack(original: Message, code: str = "AA", text: str = "") -> Message:
    """Build an ACK. code: AA accept, AE error, AR reject."""
    msh = original.first("MSH")
    if msh is None:
        raise HL7Error("cannot ACK a message without MSH")
    ack_msh = ["MSH", "^~\\&", msh.get(5), msh.get(6), msh.get(3), msh.get(4),
               now_ts(), "", "ACK^" + msh.component(9, 2), "ACK" + original.control_id,
               "P", msh.get(12) or "2.5.1"]
    return Message([Segment("MSH", ack_msh), Segment("MSA", ["MSA", code, original.control_id, text])])


@dataclass
class Observation:
    """A device result extracted from OBX, still in vendor terms."""
    specimen_id: str
    vendor_code: str
    value: str
    units: str
    reference_range: str
    abnormal_flags: list[str]
    status: str
    observed_at: str
    notes: list[str] = field(default_factory=list)


def extract_observations(msg: Message) -> list[Observation]:
    """Extract results from ORU^R01 or OUL^R22.

    The specimen id comes from SPM-2 (OUL^R22, LAW) or OBR-3 (ORU^R01).
    NTE segments following an OBX attach to that observation as notes.
    """
    mtype = msg.message_type
    if mtype not in ("ORU^R01", "OUL^R22"):
        raise HL7Error(f"unsupported result message {mtype}")
    specimen = ""
    results: list[Observation] = []
    for seg in msg.segments:
        if seg.name == "SPM":
            specimen = seg.component(2, 1) or seg.get(2)
        elif seg.name == "OBR" and not specimen:
            specimen = seg.component(3, 1) or seg.get(3)
        elif seg.name == "OBX":
            results.append(Observation(
                specimen_id=specimen,
                vendor_code=seg.component(3, 1),
                value=seg.get(5),
                units=seg.component(6, 1),
                reference_range=seg.get(7),
                abnormal_flags=[f for f in seg.get(8).split("~") if f],
                status=seg.get(11),
                observed_at=seg.get(14),
            ))
        elif seg.name == "NTE" and results:
            results[-1].notes.append(seg.get(3))
    return results
