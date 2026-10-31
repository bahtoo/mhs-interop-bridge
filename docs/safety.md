# Safety model

Safety is layered by who owns the knowledge. Every layer is on the write path, so it applies to every caller.

## 1. MHS runtime (stand-in)

| Rule | Condition |
|---|---|
| `limit` | parameter outside the manifest range, or not one of its allowed values (from SiLA constraints) |
| `missing_param`, `unknown_procedure`, `unknown_device` | malformed call |
| `device_unreachable` | device offline |
| `emergency_stop` | stop engaged; any actor may engage, only a human clears (PROPOSAL) |
| `read_only_device` | the device's risk class is `patient_connected`; checked before procedure lookup (PROPOSAL) |

Routines expand into steps, and every step passes the same checks.

## 2. Driver interlocks (hardware knowledge)

| Rule | Driver | Condition |
|---|---|---|
| `quarantine` | SiLA 2 liquid handler | the position was reported in `FoamDetected` |
| `capacity` | SiLA 2 liquid handler | the target well would exceed 300 uL |
| `plate_location` | liquid handler, plate reader | the labware is not at the device |
| `specimen_quarantine` | analyzer | the specimen's last results carried foam, clot or short sample, unless the analyzer logged that it caused the flag and has recovered |
| `maintenance_due` | analyzer | a patient specimen while the start-of-day maintenance is due |
| `reset_limit` | analyzer | a remote reset of a hardware fault beyond the profile's limit |
| `tube_cold` | CT | an exposure (air calibration, QA scan) before the tube warm-up |
| `gantry_not_clear` | CT | an air calibration with anything in the gantry, or a release with the phantom on the table |
| `no_phantom` | CT | a QA scan without the water phantom on the table |
| `qa_not_passed` | CT | release for clinical use without a QA pass since the last air calibration |

Devices also refuse on their own, through defined errors: `DestinationUnavailable` (plate mover), `InsufficientVolume`, `PositionNotFound`, and `ASTM_SESSION_ABORTED` after six NAKs.

## 3. Applications (domain rules)

| App | Rule | Condition |
|---|---|---|
| lab record | `data_integrity` | saturated or foamed wells, or R2 below 0.98 |
| clinical | `result_held` | driver hold (unknown unit, unmapped code, instrument condition) or patient mismatch with the order |
| clinical | `order_state` | the result belongs to an order that was never dispatched |
| clinical | `qc_gate` | latest QC fails Westgard 1_3s, 2_2s or R_4s |
| clinical | `critical_unacknowledged` | critical value not yet telephoned by a human |
| clinical | `unroutable`, `already_dispatched` | order cannot be, or has been, dispatched |
| clinical | `vitals_held`, `already_charted` | artefact, lead-off, unassociated monitor, or already charted |
| clinical | `data_exception` | the order has an unresolved data exception, such as a missing accession |
| clinical | `instrument_fault` | the analyzer logged an error during the run of the specimen being released |
| clinical | `instrument_suspected` | a recollection for a specimen whose run the analyzer logged an error on |
| clinical | `analyzer_down`, `not_needed` | an order routed to a stopped analyzer (reroute it); a reroute when every routed analyzer is in service |
| service | `no_service_access`, `unknown_case`, `case_closed`, `wrong_device` | the device has no service surface, or the case is unknown, closed or for another device |
| service | `not_hands_on` | a work instruction for a task the device does not list as hands-on |
| service | `part_not_indicated`, `remote_first`, `already_dispatched` | a dispatch whose part matches no logged fault, before the one remote reset the catalogue asks for, or a second one |
| service | `not_recovered`, `qc_not_passing` | closing a case with a sensor out of range, or QC or daily QA not passing |
| service | needs approval | `request_work` (the person who does the work confirms it) and `dispatch_engineer` (a service lead) |
| clinical | needs approval | every procedure: `dispatch_order`, `reroute_order`, `release_results`, `request_recollection`, `request_critical_callback`, `chart_vitals`, `propose_resolution` |

## 4. Operator channel

Agents can raise drafts, check their status and escalate. Approve, reject, revise and clear-stop exist only on the human side. A rejected draft executes nothing, and an approved draft is re-checked before it runs.

## 5. Review queue

Every clinical message is drafted before it is sent, and in copilot mode so is every device invoke. A draft holds the rendered payload, its SHA-256 and the gates it passed, and waits in `PENDING_REVIEW` for a named human. Each decision is an approval event, frozen and hash-chained to the one before.

| Rule | Condition |
|---|---|
| `not_approved` | no intact human approval event for this draft at this hash; also when the chain does not verify |
| `payload_changed` | the content re-rendered at execution differs from what was approved (for example new results, or a new vital-signs set) |
| any gate above | gates run again at execution; an approved draft can still end `EXECUTION_BLOCKED` |

The emergency stop is never drafted: anyone may engage it immediately, in every mode. See [clinical-bridge.md](clinical-bridge.md#the-review-queue).

## Modes

| Mode | Device invokes by an agent | Clinical messages |
|---|---|---|
| `autonomous` (default) | run at once, behind layers 1 and 2 | drafted for review |
| `copilot` (`serve --mode copilot`) | drafted for review; on approval they run under the approver's name, with layers 1 and 2 checked again | drafted for review |

## Invariants

See [evals.md](evals.md). They are the independent check on all of the above.

## Known limitations

* The operator channel asserts the approver's name; a deployment would authenticate it and tie it to a clinical role. The hash chain makes a changed record detectable; it does not prove who wrote it.
* Approval events are chained but not signed with a key, and drafts live in the server's memory: a restarted server keeps the event log, not the pending drafts.
* The physical model is intentionally small.
* Patient identity is checked at result time, not at specimen receipt.
* Device timestamps without a zone are treated as UTC.
