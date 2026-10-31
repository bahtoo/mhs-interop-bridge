# Proposals for MHS

Each idea below is implemented in this repository so it can be judged on running code. None of them is a claim about what MHS does today. They are offered as feedback from building drivers and evals against the public description.

## 1. Import SiLA 2 Feature Definitions

**Problem.** Many automated labs already describe their instruments in SiLA 2. Writing an MHS driver by hand for each one repeats work the Feature Definition has already done.

**Proposal.** An MHS importer that maps SiLA 2 Commands to procedures, constraints (MinimalInclusive, MaximalInclusive, Unit, Set) to enforced limits, Defined Execution Errors to documented errors, and Properties to states. The lab adds only what the FDL cannot carry: operating knowledge, error hints, interlocks and routines.

**Here.** `protocols/sila2.py`, `drivers/sila2.py`, `mhs-bridge sila-preview <file>`.

## 2. A risk class on every manifest

**Problem.** Some devices should never be controlled by an agent through a general interface: anything connected to a patient, and perhaps some high-energy equipment. Today that would rely on each driver choosing not to expose procedures.

**Proposal.** A declared `risk_class` in the manifest. The runtime enforces `patient_connected` as read-only before procedure lookup, so a procedure added to such a driver by mistake still cannot be invoked. The class is shown in discovery and in the reference file.

A second class, `patient_exposing`, marks equipment that irradiates patients (the CT). It is not refused outright: its manifest carries service and QA procedures and no patient scan, and its driver's interlocks keep those to an empty gantry or the QA phantom.

**Here.** `mhs_shim/runtime.py` (`read_only_device`), `profiles/vitalwave_m3.yaml`, `profiles/medflo_ci2.yaml` (the contrast injector), `profiles/helix_ct64.yaml`.

## 3. Error hints in the manifest

**Problem.** The Genentech case in the MHS preview shows the model treating a physical failure (foam) as a software error. The device knew it was physical; the agent did not.

**Proposal.** Each defined error can carry a short hint written by someone who runs the instrument: whether the failure is physical, and what a scientist would do. The hint appears in the reference file and in the error response.

**Here.** `error_hints` in device profiles; `drivers/sila2.py` adds them to errors and responses.

## 4. Driver interlocks that learn from device errors

**Problem.** Limits on parameters are static. Some unsafe actions only become unsafe after something happens, such as aspirating from a well the device has just reported foam in.

**Proposal.** Driver hooks before a call (veto) and after a call (learn from the response). Reusable interlocks configured per device: quarantine after an error, capacity, labware location.

**Here.** `drivers/interlocks.py`, `precheck` and `after` in the runtime.

## 5. A human-only decision channel

**Problem.** Some acts need a named, accountable person: clearing an emergency stop, telephoning a critical result, validating vital signs before charting. If the only interface is the agent's, the agent can grant its own request, or assert that a person did something they did not.

**Proposal.** A separate operator channel. Agents can request approval and check its status. Approving, denying and clearing the stop exist only on the human side. Where the act is the human's (a callback, a bedside check), the agent's request carries no claim that it happened; the approval records who did it.

Claude found the gap in an earlier design. Asked to release a critical glucose, it refused to call a procedure described as "record that a clinician was told", because nobody had been told, and escalated instead. The procedure is now `request_critical_callback`.

**Here.** `apps/operator.py`, the file-backed channel in `mcp_server.py`, and the `approve`, `deny`, `revise` and `clear-estop` CLI commands. Extended in v0.5.0 into a review queue for everything the clinical bridge sends; see 10.

## 6. Invariant-based physical safety evals

**Problem.** A safety rule tested only through its own decisions cannot reveal its own bugs.

**Proposal.** Pair each safety rule with an invariant that reads physical ground truth (what the hardware did, what reached a record) and never calls the rule. Include a guardrails-off ablation in every suite to prove the invariants detect real violations. The ablation removes automated checks, not the human decision channel.

**Here.** `evals/invariants.py`, `evals/harness.py`. In development this caught a gate that let results be released against an order that was never dispatched.

## 7. An observable emergency stop

**Problem.** An engaged emergency stop was visible only as a refusal. Discovery reported every device online and status reads looked normal, so an agent learned the lab was stopped only from its first blocked call, and with the rule removed, not at all.

**Proposal.** The stop is part of every device's discovery record (`emergency_stop`) and a banner at the top of every reference file while it is engaged. An agent can then plan around it before acting, and an eval can tell an agent that ignored a visible stop from one that could not see it.

**Here.** `Runtime.discover` and `Runtime.describe` in `mhs_shim/runtime.py`. Found by reading Claude's transcripts: with guardrails off, Claude ran the dilution during a stop it had no way to see.

**Extended in v0.4.0.** Visible was not enough. On v0.3.2, Sonnet read the stop in discovery and in three reference files in all six runs, and still went straight to invoking; with guardrails off it ran the whole task and reported success without mentioning the stop. While the stop is engaged, every tool result an agent sees now starts with a stop notice, reads included (`render` in `tools.py`). The open question this tests is whether the problem is salience or priority.

## 8. Method and deviations in the record

**Problem.** An agent that departs from a protocol for a good reason (a gentler flow rate for a viscous sample) may explain itself only in its chat report. The record a reviewer reads shows the result without the method.

**Proposal.** Applications that write records take the method from the audit trail, not from the caller, and accept a note for deviations and their reason (ALCOA+: attributable, original, complete).

**Here.** `accept_series` in `apps/lab_record.py` keeps the flow rates and mix cycles actually used for the row and an optional note.

## 9. Check before acting

**Problem.** An agent unsure whether a safety control is active has one way to find out: try the action. Haiku did exactly that on v0.3.0 and v0.3.2 ("I'll attempt the release, which the bridge should gate"; "I'll attempt the first real step... If the stop is active, the call will be refused"). With enforcement in place the block saves it. Without enforcement, the probe is the incident: a critical result reached the EHR before anyone was phoned, and a plate moved during an emergency stop.

**Proposal.** A side-effect-free check for every invoke: would it be allowed, and if not, which rule and why? The check runs the same rules as the invoke, is recorded in the audit trail as a check, and changes nothing. For applications it also says whether the act would need a human's approval. A routine is checked as submitted; its steps are checked again as they run, because later steps depend on earlier ones.

**Here.** `Runtime.preview` in `mhs_shim/runtime.py`; the `mhs_check` and `app_check` tools in `tools.py` and the MCP server. Evals count checks per run, so whether a model asks instead of probes is measurable.


## 10. Reviewed drafts bound to what is sent

**Problem.** An approval is only as good as its link to what happens next. "A human approved the release" says little if the results changed between approval and sending, if the approval record can be edited afterwards, or if the reviewer saw the agent's summary instead of the message itself.

**Proposal.** An act a human must own is a draft: the gates run, the real payload is rendered and hashed, and the draft waits for review. The reviewer sees the source intent beside the structured mapping, with the origin of each field. Approval is an append-only, hash-chained event that names the payload hash. At execution the gates run again and the content is rendered again; if it no longer matches the approved hash, nothing is sent. What is sent carries the approver's signature (FHIR Provenance), and an invariant recomputes the hash from what arrived. Missing or ambiguous data becomes an exception with unverified suggestions, never a filled-in value.

**Here.** `apps/operator.py` (drafts, state machine, approval events), `apps/clinical/bridge.py` (every procedure drafted), `apps/device_review.py` (copilot mode), `protocols/exceptions.py`, invariants I13 and I14. Prompted by an outside review asking for a human-in-the-loop co-pilot. The review asked for autonomous execution to be removed; this keeps device autonomy as the default mode, because the MHS question is how agents operate instruments safely, and puts every outbound clinical message behind review in both modes.
