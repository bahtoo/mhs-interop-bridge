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
| `specimen_quarantine` | analyzer | the specimen's last results carried foam, clot or short sample |

Devices also refuse on their own, through defined errors: `DestinationUnavailable` (plate mover), `InsufficientVolume`, `PositionNotFound`, and `ASTM_SESSION_ABORTED` after six NAKs.

## 3. Applications (domain rules)

| App | Rule | Condition |
|---|---|---|
| lab record | `data_integrity` | saturated or foamed wells, or R2 below 0.98 |
| clinical | `result_held` | driver hold (unknown unit, unmapped code, instrument condition) or patient mismatch with the order |
| clinical | `order_state` | the result belongs to an order that was never dispatched |
| clinical | `qc_gate` | latest QC fails Westgard 1_3s, 2_2s or R_4s |
| clinical | `critical_unacknowledged` | critical value without a human-approved acknowledgement |
| clinical | `unroutable`, `already_dispatched` | order cannot be, or has been, dispatched |
| clinical | `vitals_held`, `already_charted` | artefact, lead-off, unassociated monitor, or already charted |
| clinical | needs approval | `acknowledge_critical`, `chart_vitals` |

## 4. Operator channel

Agents can request approval, check its status and escalate. Approve, deny and clear-stop exist only on the human side. A denied request executes nothing, and an approved request is re-checked before it runs.

## Invariants

See [evals.md](evals.md). They are the independent check on all of the above.

## Known limitations

* The operator channel asserts the approver's name; a deployment would authenticate it and tie it to a clinical role.
* The physical model is intentionally small.
* Patient identity is checked at result time, not at specimen receipt.
* Device timestamps without a zone are treated as UTC.
