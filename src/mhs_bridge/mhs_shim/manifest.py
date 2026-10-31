"""Device manifests and the agent-facing reference file.

Public MHS description: each device is described as states (what can be read)
and procedures (what can be done), with natural-language tags that capture
knowledge not visible in code, and a generated reference file stating what the
device measures, what can be adjusted, and which safety limits are enforced.

PROPOSAL fields, not known to be in MHS: ``risk_class`` and per-procedure
``requires_presence``-style safety notes. They are rendered in the reference
file so an agent and a reviewer can see them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Param:
    name: str
    type: str
    description: str
    min: float | None = None
    max: float | None = None
    unit: str = ""
    required: bool = True
    allowed: list[str] = field(default_factory=list)


@dataclass
class State:
    name: str
    type: str
    description: str


@dataclass
class Procedure:
    name: str
    description: str
    params: list[Param] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)      # error id -> meaning
    safety_checks: list[str] = field(default_factory=list)
    routine: bool = False        # chained steps, each individually checked (MHS "code file" idea)


@dataclass
class Manifest:
    device_id: str
    device_type: str
    vendor: str
    model: str
    interface: str               # the open protocol the driver speaks to the device
    states: list[State]
    procedures: list[Procedure]
    tags: list[str] = field(default_factory=list)
    risk_class: str = ""         # PROPOSAL: patient_connected, patient_exposing, specimen_processing, research_automation
    driver: str = ""             # driver that produced this manifest
    version: str = "0.1.0"

    def to_dict(self) -> dict:
        return asdict(self)

    def procedure(self, name: str) -> Procedure | None:
        return next((p for p in self.procedures if p.name == name), None)

    def reference(self) -> str:
        lines = [f"# {self.device_id}: {self.vendor} {self.model}", "",
                 f"Type: {self.device_type}. Interface: {self.interface}. Driver: {self.driver}.",
                 f"Risk class: {self.risk_class or 'not declared'}.", "", "## Readable states"]
        lines += [f"- `{s.name}` ({s.type}): {s.description}" for s in self.states]
        lines += ["", "## Procedures"]
        if not self.procedures:
            lines += ["None. This device is read-only to every actor.", ""]
        for p in self.procedures:
            lines.append(f"### `{p.name}`" + (" [routine]" if p.routine else ""))
            lines.append(p.description)
            for prm in p.params:
                rng = ""
                if prm.min is not None or prm.max is not None:
                    lo = "-inf" if prm.min is None else f"{prm.min:g}"
                    hi = "inf" if prm.max is None else f"{prm.max:g}"
                    rng = f", enforced range {lo} to {hi}{(' ' + prm.unit) if prm.unit else ''}"
                if prm.allowed:
                    rng += f", one of {prm.allowed}"
                opt = "" if prm.required else ", optional"
                lines.append(f"- `{prm.name}` ({prm.type}{opt}{rng}): {prm.description}")
            if p.errors:
                lines.append("Defined errors: " + "; ".join(f"`{k}`: {v.rstrip('.')}" for k, v in p.errors.items()) + ".")
            if p.safety_checks:
                lines.append("Safety checks: " + "; ".join(p.safety_checks) + ".")
            lines.append("")
        if self.tags:
            lines += ["## Operating knowledge", ""] + [f"- {t}" for t in self.tags]
        return "\n".join(lines).rstrip() + "\n"
