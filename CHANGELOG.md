# Changelog

Versions follow semantic versioning.

## [0.1.0] - 2026-10-19

First public version.

### Added
- `mhs_shim`: a minimal stand-in for the MHS runtime (discovery, read and invoke, manifest limits and allowed values, routines, emergency stop, audit), limited to the public description, with extensions marked PROPOSAL.
- SiLA 2 Feature Definition parser and a generic SiLA 2 driver that generates MHS manifests from FDL; Feature Definitions for a liquid handler and a plate mover.
- File-drop driver for instruments without an API, shown with an absorbance plate reader.
- Clinical analyzer driver for ASTM E1381/E1394 and HL7 v2 (OUL^R22 results, OML^O33 and ASTM host orders), with dialect profiles normalising to LOINC and UCUM.
- Read-only bedside monitor driver (HL7 ORU^R01, IEEE 11073 terms).
- Reusable interlocks (quarantine on error, capacity, labware location) and the SerialDilution routine.
- Applications on MHS: lab record (data-integrity gate), clinical bridge (FHIR ServiceRequest in; Task, Observation, DiagnosticReport and Communication out), and an operator channel where only humans decide.
- 23 eval scenarios across research and clinical tracks, measuring capability and safety; twelve independent invariants; guardrails-off ablation; naive, reference and Claude agents.
- MCP server with eleven tools and file-backed human and EHR channels; `mhs-bridge` CLI.
- Docs: architecture, evals, safety, proposals for MHS, clinical bridge, and four cookbooks.
- 55 tests.
