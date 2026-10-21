# Changelog

Versions follow semantic versioning.

## [0.3.1] - 2026-10-21

Two fixes from the Haiku verification run on v0.3.0. Scripted results are unchanged.

### Fixed
- Clinical-release scenarios now say the specimen came with a paper request form and has no EHR order, and that the bridge releases it against the patient on its label. Both Sonnet and Haiku had cited the missing order as a reason to stop, which the task never explained.
- A release that does not cover every ordered test no longer marks the order complete. The EHR Task stays `in-progress` with the outstanding LOINC codes as its reason, and the DiagnosticReport status is `partial`. Only reachable when the routing gate is off, since dispatch refuses a partly unroutable order. Found by Haiku in the guardrails-off ablation.
- 66 tests (was 65).

## [0.3.0] - 2026-10-21

Fixes found by reading the transcripts of the first full Claude run (138 runs, claude-sonnet-5-5). Four of its five failure groups came from the harness or scenario design, not the model. Scripted results change where noted.

### Changed (breaking)
- `acknowledge_critical` is replaced by `request_critical_callback(result_id, recipient?, context?)`. The agent asks a human to telephone the critical value; approving records that the human did it, under their name, in the FHIR Communication. The agent no longer supplies a note asserting that someone was told. Invariant I2 and the reference agent follow.

### Fixed
- The guardrails-off ablation kept removing the human-approval channel, so an agent's request for a nurse's validation charted vital signs directly and was scored as charting without validation. The ablation now removes automated checks only. Naive with guardrails off: 1/23 tasks, 58 violations (was 0/23, 76).
- Scenario and docs wording: the foam scenario's protocol is a legacy protocol, as the task says, not a mandated one.

### Added
- PROPOSAL: the emergency stop is visible in discovery (`emergency_stop`) and as a banner on every reference file while engaged.
- PROPOSAL: `accept_series` records the flow rates, mix cycles and transfer count actually used for the row, taken from the audit trail, plus an optional deviation `note`. The lab record's operating knowledge asks for deviations to be recorded.
- Partial results: on an infrastructure failure the eval stops starting new runs and writes the finished ones as `results.partial.md`, `results.partial.json` and `transcripts.partial.jsonl` with a banner. `progress.jsonl` records each run as it finishes.
- `--scenario` takes a comma-separated list, with a clear error for an unknown name; the workflow's scenario input takes a list too. Workflow default trials is 3.
- Proposals 7 and 8 in `docs/proposals.md`.
- 65 tests (was 62).

## [0.2.1] - 2026-10-20

### Fixed
- `claude-eval` workflow: the eval's error output is now captured in the uploaded log, the run summary reports why an eval did not complete, and the reason is raised as a GitHub annotation. Previously an aborted run left an almost empty log.
- `claude-eval` workflow: fails fast with a clear message when the `ANTHROPIC_API_KEY` secret is missing.

### Added
- `claude-eval` workflow: the headline result rows are also raised as notice annotations, readable through the GitHub API.

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
