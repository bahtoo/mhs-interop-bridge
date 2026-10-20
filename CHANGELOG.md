# Changelog

Versions follow semantic versioning.

## [0.2.0] - 2026-10-20

Makes the Claude eval measurable before any Claude number is quoted. Scripted results are unchanged.

### Added
- `--trials`: repeated runs per scenario for LLM agents, reported as per-run success, pass^k and pass@k. Scripted agents still run once.
- Guardrails-off ablation for Claude, on by default when Claude is evaluated; `--ablate` chooses the agents.
- `transcripts.jsonl`: every tool call and result, Claude's reasoning text and the finish call, per run.
- `--workers` for parallel runs; each run builds its own world.
- Token usage, turn count and malformed-call count per run in `results.json`.
- Prompt caching of the system prompt, tools and conversation prefix.
- Manual `claude-eval` GitHub workflow using the `ANTHROPIC_API_KEY` secret; results uploaded as an artifact.
- Seven offline tests with a scripted stand-in for the API client (62 tests).

### Changed
- A malformed tool call is returned to the agent as a `malformed_call` error instead of ending the run as a crash.
- Infrastructure failures (no SDK, no key, API errors after retries) abort the eval with exit code 2 and write no results, instead of being scored as agent failures.
- `results.json` gains `trial`, `tool_errors`, `turns` and `usage`.

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
