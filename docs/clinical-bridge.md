# Clinical bridge (optional module)

Hospital and reference laboratories are the most regulated setting lab automation runs in, and they already have mature open interfaces: ASTM E1381/E1394 and HL7 v2 between analyzers and the laboratory information system, and FHIR R4 to the EHR. The clinical bridge is an application on MHS that shows how agent-operated instruments fit into that loop without weakening any of its controls.

It uses MHS like any client: it reads analyzer catalogues, results, QC history and monitor states, and invokes `download_order`. Everything clinical lives in the app, outside the MHS boundary.

**Nothing the bridge sends leaves without a named human's approval.** Every procedure is drafted rather than executed: the gates run, the real message is rendered (the ASTM host order or OML^O33 for each analyzer, the FHIR Bundle for the EHR, the ward Task), and the draft waits in `PENDING_REVIEW`. The agent proposes; a technician, nurse or supervisor approves, rejects or revises; the bridge sends exactly what was approved, with the approver's Provenance. See [The review queue](#the-review-queue) below.

## Order to result

```mermaid
sequenceDiagram
    participant EHR
    participant App as Clinical bridge
    participant MHS
    participant Agent
    participant Chem as Analyzer (ASTM)
    EHR->>App: ServiceRequest (LOINC, patient, specimen)
    App->>MHS: read catalogue of each analyzer
    App-->>EHR: Task requested
    participant Human as Technician
    Agent->>App: dispatch_order
    App->>App: gates; render the ASTM host order; draft (PENDING_REVIEW)
    Human->>App: approve --as name
    App->>MHS: invoke chem-01 download_order (the approved message)
    MHS->>Chem: E1381 session: H, P, O (action N), L
    App-->>EHR: Task accepted
    Agent->>MHS: run_qc, run_specimen
    Chem->>MHS: E1381 session: R records
    Agent->>App: release_results
    App->>App: holds, identity, order state, Westgard, criticals; render the Bundle; draft
    Human->>App: approve --as name
    App->>App: gates again; content still matches the approved hash
    App-->>EHR: Observations + DiagnosticReport basedOn ServiceRequest + Provenance; Task completed
```

## FHIR resources

| Direction | Resource | When |
|---|---|---|
| In | ServiceRequest | an order: LOINC tests, patient, specimen barcode, priority |
| Out | Task | order lifecycle (requested, accepted, in-progress, completed, on-hold with reason); specimen recollection |
| Out | Observation (laboratory) | released results, `basedOn` the order, LOINC and UCUM |
| Out | DiagnosticReport | per specimen, `basedOn` the order, the Task's output |
| Out | Communication | documented notification of a critical result |
| Out | Observation (vital-signs) | nurse-validated bedside vitals |
| Out | Provenance | with every message: the approver's verification signature over the draft's SHA-256 |
| Record | AuditEvent | every step of every draft (drafted, approved, rejected, superseded, executed, blocked); written by the server to `audit_events.json` for an audit repository |

All outbound messages are transaction Bundles with idempotent PUTs, tagged with the draft they came from (`meta.tag`, system `urn:mhs-bridge:review`).

## Workflows and their scenarios

| Workflow | Scenario |
|---|---|
| Order to result over ASTM, and over HL7 (OML^O33 down, OUL^R22 up) | `order_to_result_astm`, `order_to_result_hl7` |
| Critical result: held, telephoned by a human (approved callback), Communication, then release | `clin_critical_ack`, `clin_critical_denied` |
| Wrong-patient result: held, recollection Task, order on hold | `order_patient_mismatch` |
| Specimen problem: foam or clot, same cup quarantined, recollection | `clin_foam_specimen`, `hema_clot` |
| QC failure: Westgard blocks patient release | `clin_qc_failure` |
| Unit drift: unknown unit held, never rescaled; candidate readings listed for the reviewer | `hema_unit_drift` |
| Unroutable order: no substitution, escalate | `order_unroutable` |
| Missing accession: order kept with an exception and candidates; a human confirms one before dispatch | `order_missing_accession` |
| Bedside vitals: MDC to LOINC, nurse validation, artefact and association holds | `vitals_chart`, `vitals_artefact`, `vitals_unassociated` |

## The bedside monitor

The monitor's profile declares `risk_class: patient_connected` and its manifest has no procedures. Any attempt to invoke one, such as silencing an alarm or rezeroing a sensor, is refused by the runtime. Alarms stay on the monitor and the nurse call system; this path is for charting only.

## The review queue

| State | Meaning |
|---|---|
| `PENDING_REVIEW` | drafted: gates passed, payload rendered and hashed, nothing sent |
| `APPROVED` | a named human approved it; an approval event is recorded |
| `EXECUTED` | gates passed again, the content re-rendered to the approved hash, and it was sent |
| `EXECUTION_BLOCKED` | approved, but a gate now refuses or the content changed (`payload_changed`); nothing was sent |
| `REJECTED` | a human rejected it; nothing was sent |
| `SUPERSEDED` | a human revised it; the new revision is a new draft (`superseded_by`) |

Only these moves are legal: `PENDING_REVIEW` to `APPROVED`, `REJECTED` or `SUPERSEDED`, and `APPROVED` to `EXECUTED` or `EXECUTION_BLOCKED`. Anything else raises.

**Approval events** are frozen records: draft id, revision, payload SHA-256, decision (`approve`, `reject`, `revise`), approver, UTC time, note, and the hash of the previous event. Changing or removing one breaks the chain. The server appends them to `review.jsonl`, reads the file back on every sync, and applies no further decision once it stops verifying. An approved draft also refuses to run if the in-memory chain does not verify.

**What the hash covers** is the clinical content: the resources in the Bundle without send-time fields (`authoredOn`, `lastModified`, `sent`, `meta`) and without the Provenance added at send time; for an order download, the arguments sent to each analyzer; for a callback, the result and recipient. Transport envelope fields (HL7 control id, MSH-7) are set at send time. Invariant I13 recomputes the hash from what actually reached the EHR and each analyzer.

**The reviewer's view** (`Draft.review()`, `mhs-bridge show <id>`) separates what was asked for from what would be sent:

```json
{"request_id": "apr-0001", "revision": 1, "kind": "lis_order", "status": "PENDING_REVIEW",
 "intent":   {"source": {"type": "ServiceRequest", "id": "sr-1001", "text": "stat ServiceRequest ...", "raw": {}},
              "agent_rationale": "stat order, both tests routable to chem-01"},
 "proposed": {"fields": [{"path": "chem-01.specimen_id", "value": "S-1001",
                          "from": "intent.source.raw.specimen[0].identifier.value"}]},
 "payload":  {"format": "lis_order", "text": "H|\\^&|||HOST^LIS ...", "content": {}, "sha256": "9e21..."},
 "exceptions": [], "gates": {"allowed": true, "checked": []}, "history": []}
```

Each proposed field says where its value came from, so a dashboard can put the source and the mapping side by side and redline per field. A revised field carries `changed_in_revision`. The schema is in [review-schema.json](review-schema.json).

## Missing and ambiguous data

The bridge never fills a gap. A gap is a hold or a blocked dispatch, and it carries a data exception: the field, the issue, and **suggestions**, each with its basis, its evidence and a plausibility flag. Suggestions are never marked verified and never applied automatically.

| Gap | What happens | Suggestions |
|---|---|---|
| ServiceRequest without a specimen | order kept, `dispatch_order` blocked (`data_exception`) | an accession-type identifier on the request (`ACSN`); the requisition number (implausible, check the tube); "read the barcode" |
| Result in an unknown unit | result held (`unit_mismatch`) | each reading of the value (a known conversion from the profile, a mislabelled vendor unit, already in the target unit), ranked by the test's reportable range |

An agent may propose a value with `propose_resolution(order_id, exception_id, value, basis)`. That is a draft like any other; a human confirms it, edits it with `revise`, or rejects it, and only then does the order change. A result in an unknown unit is not resolvable through the bridge: the suggestions help whoever fixes the interface mapping, and the specimen is then re-run.
