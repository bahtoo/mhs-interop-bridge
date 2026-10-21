# Clinical bridge (optional module)

Hospital and reference laboratories are the most regulated setting lab automation runs in, and they already have mature open interfaces: ASTM E1381/E1394 and HL7 v2 between analyzers and the laboratory information system, and FHIR R4 to the EHR. The clinical bridge is an application on MHS that shows how agent-operated instruments fit into that loop without weakening any of its controls.

It uses MHS like any client: it reads analyzer catalogues, results, QC history and monitor states, and invokes `download_order`. Everything clinical lives in the app, outside the MHS boundary.

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
    Agent->>App: dispatch_order
    App->>MHS: invoke chem-01 download_order
    MHS->>Chem: E1381 session: H, P, O (action N), L
    App-->>EHR: Task accepted
    Agent->>MHS: run_qc, run_specimen
    Chem->>MHS: E1381 session: R records
    Agent->>App: release_results
    App->>App: holds, identity, order state, Westgard, criticals
    App-->>EHR: Observations + DiagnosticReport basedOn ServiceRequest; Task completed
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

All outbound messages are transaction Bundles with idempotent PUTs.

## Workflows and their scenarios

| Workflow | Scenario |
|---|---|
| Order to result over ASTM, and over HL7 (OML^O33 down, OUL^R22 up) | `order_to_result_astm`, `order_to_result_hl7` |
| Critical result: held, telephoned by a human (approved callback), Communication, then release | `clin_critical_ack`, `clin_critical_denied` |
| Wrong-patient result: held, recollection Task, order on hold | `order_patient_mismatch` |
| Specimen problem: foam or clot, same cup quarantined, recollection | `clin_foam_specimen`, `hema_clot` |
| QC failure: Westgard blocks patient release | `clin_qc_failure` |
| Unit drift: unknown unit held, never rescaled | `hema_unit_drift` |
| Unroutable order: no substitution, escalate | `order_unroutable` |
| Bedside vitals: MDC to LOINC, nurse validation, artefact and association holds | `vitals_chart`, `vitals_artefact`, `vitals_unassociated` |

## The bedside monitor

The monitor's profile declares `risk_class: patient_connected` and its manifest has no procedures. Any attempt to invoke one, such as silencing an alarm or rezeroing a sensor, is refused by the runtime. Alarms stay on the monitor and the nurse call system; this path is for charting only.
