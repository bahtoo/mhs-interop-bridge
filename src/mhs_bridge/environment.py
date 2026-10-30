"""Assemble a complete, simulated environment.

    world (physical ground truth)
      -> simulated instruments, each speaking its own protocol
      -> drivers (this repository's contribution)
      -> MHS runtime (stand-in)
      -> applications (lab record, clinical bridge) and the operator channel

``faults`` is keyed by device id (plus ``mhs`` for the runtime), for example
``{"lh-01": {"offline": True}, "chem-01": {"qc_shift": {"GLU": 3.5}}, "mhs": {"estop": True}}``.

``mode`` is ``autonomous`` (an agent's device invokes run at once, behind limits and interlocks) or ``copilot``
(they are drafted for a human's review). Clinical messages are reviewed in both modes.

``knowledge="off"`` removes the operating knowledge a lab writes into device profiles: the natural-language
tags, the hint attached to each defined error, and the hint on an interlock refusal. What is left is what the
instrument itself reports: its Feature Definition, limits, errors, states and data. An eval then measures what
the agent infers about the physical world, not what it reads.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .apps.clinical.bridge import ClinicalBridgeApp
from .apps.clinical.ehr import MemoryEhr
from .apps.device_review import DeviceReview
from .apps.lab_record import LabRecordApp
from .apps.service import ServiceApp
from .apps.operator import OperatorChannel
from .drivers.analyzer import AnalyzerDriver
from .drivers.dialects import Profile, builtin_profile_data
from .drivers.filedrop import FileDropReaderDriver
from .drivers.monitor import MonitorDriver
from .drivers.sila2 import SiLA2Driver, load_builtin_feature
from .mhs_shim import Runtime
from .sim.clinical_instruments import SimAnalyzer, SimBedsideMonitor
from .sim.lab_instruments import SimFileDropReader, SimLiquidHandler, SimPlateMover
from .sim.world import LabWorld

SILA_SIMULATORS = {"liquid_handler": SimLiquidHandler, "plate_mover": SimPlateMover}


@dataclass
class Environment:
    world: LabWorld
    runtime: Runtime
    operator: OperatorChannel
    lab: LabRecordApp
    clinical: ClinicalBridgeApp
    ehr: Any
    folder: Path
    devices: DeviceReview
    service: ServiceApp
    mode: str = "autonomous"
    knowledge: str = "on"

    @property
    def apps(self) -> dict[str, Any]:
        return {self.lab.app_id: self.lab, self.clinical.app_id: self.clinical, self.service.app_id: self.service}


MODES = ("autonomous", "copilot")
KNOWLEDGE = ("on", "off")


def _without_knowledge(data: dict) -> dict:
    return {**data, "tags": [], "error_hints": {}}


def build(faults: dict | None = None, folder: str | Path | None = None, audit_path: str | Path | None = None,
          ehr: Any | None = None, mode: str = "autonomous", knowledge: str = "on") -> Environment:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if knowledge not in KNOWLEDGE:
        raise ValueError(f"knowledge must be one of {KNOWLEDGE}, not {knowledge!r}")
    faults = faults or {}
    folder = Path(folder or tempfile.mkdtemp(prefix="mhs-bridge-"))
    world = LabWorld.default()
    runtime = Runtime(audit_path)
    for dev_id, data in builtin_profile_data().items():
        data = _without_knowledge(data) if knowledge == "off" else data
        f = faults.get(dev_id, {})
        world.online[dev_id] = not f.get("offline", False)
        driver = data.get("driver")
        if driver == "sila2":
            sim = SILA_SIMULATORS[data["device_type"]](world, dev_id)
            if f.get("lld_off"):
                sim.lld = False
            runtime.register(SiLA2Driver(data, sim, [load_builtin_feature(x) for x in data["features"]],
                                         is_online=lambda d=dev_id: world.online[d]))
        elif driver == "filedrop":
            sim = SimFileDropReader(world, folder / dev_id, dev_id)
            runtime.register(FileDropReaderDriver(data, folder / dev_id, sim.process_inbox,
                                                  is_online=lambda d=dev_id: world.online[d]))
        elif data.get("device_type") == "patient_monitor":
            runtime.register(MonitorDriver(Profile(data), SimBedsideMonitor(Profile(data), f)))
        elif data.get("protocol") in ("astm", "hl7"):
            runtime.register(AnalyzerDriver(Profile(data), SimAnalyzer(Profile(data), f)))
    if knowledge == "off":
        for d in runtime.drivers.values():
            for lock in getattr(d, "interlocks", []):
                if hasattr(lock, "hint"):
                    lock.hint = ""          # the refusal still happens; only the explanation is gone
    if faults.get("mhs", {}).get("estop"):
        runtime.engage_estop("scenario", "engaged before the run")
    operator = OperatorChannel(runtime)
    ehr = ehr or MemoryEhr()
    lab = LabRecordApp(runtime, operator)
    clinical = ClinicalBridgeApp(runtime, operator, ehr)
    return Environment(world, runtime, operator, lab, clinical, ehr, folder, DeviceReview(runtime, operator),
                       ServiceApp(runtime, operator), mode, knowledge)
