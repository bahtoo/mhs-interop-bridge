# Architecture

## The boundary

Anthropic's description of MHS covers a standardized driver with read and write primitives, discovery of devices in a standard format, natural-language tags that generate a reference file, device-level safety limits, a shared state view (the Janelia state dictionary), orchestration across devices (Carnegie Mellon, University of Washington), and agent access over MCP, a CLI and code files.

That is the bus. This repository does not rebuild it. It works on the three things around it that a lab deployment needs:

| Layer | Owner | Here |
|---|---|---|
| Agent | Anthropic, or any MCP client | Claude in the evals; any MCP client through `mcp_server.py` |
| **MHS runtime** | Anthropic (open source later) | `mhs_shim/`: a stand-in mirroring only the public description |
| **Drivers** | Labs, vendors, integrators | `drivers/`: the main contribution |
| **Applications on MHS** | Labs | `apps/`: lab record, clinical bridge, operator channel and review queue |
| Instruments | Vendors | `sim/`: simulated instruments speaking real protocols |

## What the stand-in does, and why it is small

`mhs_shim/runtime.py` provides registry and discovery, `read` and `invoke`, enforcement of manifest parameter ranges and allowed values, routines that expand into individually checked steps, an emergency stop, and an audit trail. The manifest and reference file (`mhs_shim/manifest.py`) follow the published shape: states, procedures and natural-language tags, rendered into what the device measures, what it can adjust and which limits apply.

Five things in the stand-in go beyond the public description and are marked PROPOSAL: the risk class, driver interlock hooks (`precheck` and `after`), the rule that only a human clears the emergency stop, the stop's visibility in discovery and reference files, and the side-effect-free check (`preview`). They are collected in [proposals.md](proposals.md).

Domain rules are deliberately absent from the stand-in. Whether a clinical result may reach a patient record, or whether a research curve is valid, is an application decision.

## Drivers

| Driver | Protocol | Generated from | Interlocks |
|---|---|---|---|
| `drivers/sila2.py` | SiLA 2 | the instrument's Feature Definitions, plus a device profile | configured per device |
| `drivers/filedrop.py` | command file in, CSV export and status file out | a device profile | plate presence from the status file |
| `drivers/analyzer.py` | ASTM E1381/E1394 or HL7 v2 (OUL^R22, OML^O33) | a dialect profile | specimen quarantine after a physical flag |
| `drivers/monitor.py` | HL7 v2 ORU^R01, IEEE 11073 terms | a dialect profile | none needed: no procedures, `patient_connected` |

### The SiLA 2 split

A Feature Definition describes the interface: commands, parameters, constraints, errors and properties. It does not describe how an experienced scientist uses the instrument. The driver therefore takes two inputs:

* **Feature Definition** (standard, from the vendor): becomes procedures, enforced ranges and units, allowed values, documented errors and states.
* **Device profile** (lab knowledge, a short YAML file): adds operating knowledge, a hint per defined error, interlocks, routines and the risk class.

A lab with SiLA 2 instruments writes only the second.

### Interlocks

`drivers/interlocks.py` holds reusable hardware knowledge applied on the write path:

* `quarantine_on_error`: after a defined error names a position, refuse further calls that touch it. This prevents the same-well retry after foam.
* `capacity`: refuse a transfer that would overfill the target. The hardware will not stop it.
* `on_deck`: refuse a call that names labware not at this device.

They live in drivers because they are about the device, whoever is calling and whatever application is running.

### Routines

`drivers/routines.py` implements chained commands, the MHS code-file idea. `SerialDilution` expands into Transfer steps. The runtime runs each step through the same checks as a direct call and stops at the first failure.

## Applications

Applications use MHS the way any client does: they discover, read and invoke. They also own the rules that are not about hardware.

* **Lab record** (`apps/lab_record.py`) accepts a dilution row only if no well is saturated or foamed and R2 is at least 0.98. It combines the liquid handler's plate map (expected concentration, foam) with the reader's export (absorbance, saturation). No single device can make that judgement.
* **Clinical bridge** (`apps/clinical/`) handles FHIR orders in, capability routing over analyzer catalogues, order download, release gates, critical-result notification, recollection, vital-sign charting and FHIR out. See [clinical-bridge.md](clinical-bridge.md).
* **Operator channel** (`apps/operator.py`) holds the review queue, escalations and the emergency-stop clear. Every clinical message, and in copilot mode every device invoke (`apps/device_review.py`), is a draft that a named human approves, rejects or revises. Decisions are hash-chained approval events, and a draft runs only if its content still matches the approved hash. Agents can request; only humans decide.

## Tool surface

`tools.py` defines thirteen tools in three groups: `mhs_*` (what MHS would give an agent), `app_*` and `operator_*`. `mhs_check` and `app_check` run the same checks and gates as the matching invoke without acting (PROPOSAL 9). The MCP server and the eval agent use the same definitions and the same response text. No tool approves, rejects, revises or clears the stop. In copilot mode (`serve --mode copilot`) `mhs_invoke` drafts instead of acting.

## Simulation

`sim/world.py` is physical ground truth: plates, wells, volumes, concentrations, foam, plate locations and readings. Simulated instruments act on it and speak their real protocols: SiLA 2 call shapes, files on disk, E1381 frames, MLLP. The physical model is small: viscous protein above 25 ug/mL foams above 50 uL/s or with more than 3 mix cycles, bubbles add optical error, and absorbance saturates at 3.0. The eval invariants read the world directly.
