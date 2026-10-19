"""ASTM E1381 (CLSI LIS1-A) low-level framing and ASTM E1394 (CLSI LIS2-A2) records.

Implemented from the public standard descriptions only. No vendor specification
is used. Vendor-specific behaviour lives in YAML profiles (see ``profiles/``).

Low level (E1381):
    <STX> FN text <ETB|ETX> C1 C2 <CR> <LF>
    FN      frame number, ASCII '1'..'7' then '0', cycling
    C1 C2   checksum: sum of bytes from FN through ETB/ETX inclusive, mod 256,
            as two upper-case hex characters
    Text is at most 240 characters per frame; longer records are split with ETB.
    Session: ENQ -> ACK, frames each answered with ACK or NAK, EOT to finish.
    A frame NAKed six times aborts the session (E1381 retry limit).

High level (E1394):
    Records H, P, O, R, C, Q, L separated by <CR>. Delimiters are declared in
    the header: field '|', repeat '\\', component '^', escape '&'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

ENQ, ACK, NAK, EOT = b"\x05", b"\x06", b"\x15", b"\x04"
STX, ETX, ETB = b"\x02", b"\x03", b"\x17"
CR, LF = b"\r", b"\n"

MAX_FRAME_TEXT = 240
MAX_RETRIES = 6


class ASTMError(Exception):
    """Raised for framing, checksum or session violations."""


# ---------------------------------------------------------------------------
# E1381 framing
# ---------------------------------------------------------------------------

def checksum(body: bytes) -> bytes:
    """Checksum over FN + text + ETB/ETX, returned as two upper-case hex bytes."""
    return f"{sum(body) % 256:02X}".encode("ascii")


def build_frames(message: str) -> list[bytes]:
    """Split an E1394 message (records joined by CR) into E1381 frames.

    Each record is framed separately; a record longer than 240 characters is
    split into intermediate (ETB) frames followed by a final (ETX) frame.
    """
    frames: list[bytes] = []
    fn = 1
    records = [r for r in message.split("\r") if r]
    for record in records:
        text = (record + "\r").encode("latin-1")
        chunks = [text[i:i + MAX_FRAME_TEXT] for i in range(0, len(text), MAX_FRAME_TEXT)]
        for idx, chunk in enumerate(chunks):
            terminator = ETX if idx == len(chunks) - 1 else ETB
            body = str(fn).encode("ascii") + chunk + terminator
            frames.append(STX + body + checksum(body) + CR + LF)
            fn = (fn + 1) % 8
    return frames


@dataclass
class Frame:
    number: int
    text: bytes
    final: bool


def parse_frame(raw: bytes, expected_fn: int | None = None) -> Frame:
    """Validate and decode one E1381 frame. Raises ASTMError on any defect."""
    if len(raw) < 7 or not raw.startswith(STX) or not raw.endswith(CR + LF):
        raise ASTMError("malformed frame envelope")
    body, chk = raw[1:-4], raw[-4:-2]
    if checksum(body) != chk.upper():
        raise ASTMError(f"checksum mismatch: expected {checksum(body)!r}, got {chk!r}")
    fn = body[0:1]
    if not fn.isdigit() or int(fn) > 7:
        raise ASTMError(f"invalid frame number {fn!r}")
    if expected_fn is not None and int(fn) != expected_fn:
        raise ASTMError(f"frame number {int(fn)} out of sequence, expected {expected_fn}")
    terminator = body[-1:]
    if terminator not in (ETX, ETB):
        raise ASTMError("frame missing ETX/ETB")
    return Frame(number=int(fn), text=body[1:-1], final=terminator == ETX)


def reassemble(frames: Iterable[bytes]) -> str:
    """Reassemble validated frames back into an E1394 message string."""
    out = b""
    expected = 1
    for raw in frames:
        frame = parse_frame(raw, expected_fn=expected)
        out += frame.text
        expected = (expected + 1) % 8
    return out.decode("latin-1")


@dataclass
class SessionResult:
    message: str | None
    frames_sent: int
    retransmissions: int
    aborted: bool
    log: list[str] = field(default_factory=list)


def transfer(message: str, channel: Callable[[bytes], bytes]) -> SessionResult:
    """Run a sender-side E1381 session over ``channel``.

    ``channel`` takes the bytes the sender transmits and returns the receiver's
    reply (ACK, NAK, or for ENQ possibly NAK meaning busy). This lets the
    simulator inject line noise, busy receivers and NAK storms.
    """
    log: list[str] = []
    reply = channel(ENQ)
    log.append(f"ENQ -> {reply!r}")
    if reply != ACK:
        channel(EOT)
        return SessionResult(None, 0, 0, aborted=True, log=log + ["receiver busy or contention"])

    sent, retrans = 0, 0
    accepted: list[bytes] = []
    for frame in build_frames(message):
        attempts = 0
        while True:
            reply = channel(frame)
            sent += 1
            if reply == ACK:
                accepted.append(frame)
                break
            attempts += 1
            retrans += 1
            log.append(f"frame {frame[1:2].decode()} NAK #{attempts}")
            if attempts >= MAX_RETRIES:
                channel(EOT)
                log.append("retry limit reached, session aborted")
                return SessionResult(None, sent, retrans, aborted=True, log=log)
    channel(EOT)
    return SessionResult(reassemble(accepted), sent, retrans, aborted=False, log=log)


# ---------------------------------------------------------------------------
# E1394 records
# ---------------------------------------------------------------------------

@dataclass
class Delimiters:
    field: str = "|"
    repeat: str = "\\"
    component: str = "^"
    escape: str = "&"

    @classmethod
    def from_header(cls, header: str) -> "Delimiters":
        if not header.startswith("H") or len(header) < 5:
            raise ASTMError("message must start with an H record declaring delimiters")
        return cls(field=header[1], repeat=header[2], component=header[3], escape=header[4])


@dataclass
class Record:
    type: str
    fields: list[str]

    def get(self, position: int, default: str = "") -> str:
        """Field by 1-based ASTM position (position 1 is the record type)."""
        idx = position - 1
        return self.fields[idx] if 0 <= idx < len(self.fields) else default

    def component(self, position: int, comp: int, delims: Delimiters) -> str:
        parts = self.get(position).split(delims.component)
        return parts[comp - 1] if 0 < comp <= len(parts) else ""


def parse_message(message: str) -> tuple[Delimiters, list[Record]]:
    lines = [ln for ln in message.replace("\n", "").split("\r") if ln]
    if not lines:
        raise ASTMError("empty message")
    delims = Delimiters.from_header(lines[0])
    records: list[Record] = []
    for ln in lines:
        if ln.startswith("H"):
            # The delimiter definition field is literal and must not be split.
            rest = ln[5:].split(delims.field)
            records.append(Record("H", ["H", ln[1:5]] + rest[1:]))
        else:
            parts = ln.split(delims.field)
            records.append(Record(parts[0][:1], parts))
    if records[-1].type != "L":
        raise ASTMError("message must end with an L (terminator) record")
    return delims, records


def build_message(records: list[list[str]], delims: Delimiters | None = None) -> str:
    """Build an E1394 message from field lists. The H record's delimiter field is
    generated automatically, so pass the H record without it."""
    d = delims or Delimiters()
    out = []
    for rec in records:
        if rec[0] == "H":
            out.append("H" + d.field + d.repeat + d.component + d.escape + d.field + d.field.join(rec[1:]))
        else:
            out.append(d.field.join(rec))
    return "\r".join(out) + "\r"
