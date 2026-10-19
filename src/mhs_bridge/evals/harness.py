"""Eval harness: scenarios x agents, scored, with a guardrails-off ablation.

Metrics per run:
    success     the scenario's own definition of a good outcome, and no invariant violated
    blocked     calls refused by MHS checks, driver interlocks or application gates
    violations  independent invariant failures
    approvals   human approvals requested
    tool_calls  tool calls made
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..environment import Environment, build
from ..mhs_shim import Decision
from .agents import AGENTS, SimulatedHuman, Toolbox
from .invariants import check_invariants
from .scenarios import SCENARIOS, Scenario


def disable_guardrails(env: Environment) -> None:
    """Ablation only. Keeps malformed-call checks, drops every safety rule."""
    rt = env.runtime

    def permissive(device_id: str, procedure: str, args: dict) -> Decision:
        d = rt.drivers.get(device_id)
        if d is None or d.manifest().procedure(procedure) is None:
            return Decision.block("unknown_procedure", procedure)
        return Decision.ok()

    rt.check = permissive
    env.lab.check = lambda procedure, args: Decision.ok()
    env.clinical.check = lambda procedure, args: Decision.ok()
    env.clinical.NEEDS_HUMAN = set()


@dataclass
class RunResult:
    scenario: str
    track: str
    measures: str
    agent: str
    guardrails: bool
    success: bool
    reason: str
    outcome: str
    blocked: int
    violations: list[str] = field(default_factory=list)
    approvals: int = 0
    tool_calls: int = 0
    summary: str = ""
    error: str = ""


def run_one(sc: Scenario, agent_name: str, guardrails: bool = True, **agent_kwargs) -> RunResult:
    env = build(sc.faults)
    for sr in sc.ehr_orders:
        env.clinical.receive_order(sr)
    if not guardrails:
        disable_guardrails(env)
    tb = Toolbox(env, SimulatedHuman(sc.human_approves))
    error = ""
    try:
        report = AGENTS[agent_name](**agent_kwargs).run(tb, sc)
    except Exception as exc:  # noqa: BLE001 - an agent crash is a scored failure
        report, error = {"outcome": "failed", "summary": ""}, f"{type(exc).__name__}: {exc}"
    tb.human.tick(env)
    ok, reason = sc.check(env, report)
    offline = {d for d, f in sc.faults.items() if f.get("offline")}
    violations = check_invariants(env, sc.faults.get("mhs", {}).get("estop", False), offline)
    blocked = sum(e.outcome == "blocked" for e in env.runtime.audit if e.kind in ("invoke", "app"))
    return RunResult(sc.name, sc.track, sc.measures, agent_name, guardrails, ok and not violations and not error,
                     error or reason, report.get("outcome", ""), blocked, violations,
                     len(env.operator.approvals), tb.calls, report.get("summary", "")[:300], error)


def run_suite(agents: list[str], scenarios: list[Scenario] | None = None, ablation: bool = True,
              **agent_kwargs) -> list[RunResult]:
    scenarios = scenarios or SCENARIOS
    out: list[RunResult] = []
    for name in agents:
        out += [run_one(sc, name, True, **(agent_kwargs if name == "claude" else {})) for sc in scenarios]
    if ablation and "naive" in agents:
        out += [run_one(sc, "naive", False) for sc in scenarios]
    return out


def _label(r: RunResult) -> str:
    return r.agent + ("" if r.guardrails else " (guardrails off)")


def to_markdown(results: list[RunResult]) -> str:
    labels = list(dict.fromkeys(_label(r) for r in results))
    by = {(r.scenario, _label(r)): r for r in results}
    lines = ["| Scenario | Track | Measures | " + " | ".join(labels) + " |",
             "|---|---|---|" + "|".join("---" for _ in labels) + "|"]
    for s in dict.fromkeys(r.scenario for r in results):
        first = next(r for r in results if r.scenario == s)
        cells = []
        for lab in labels:
            r = by.get((s, lab))
            cells.append("" if r is None else f"{'pass' if r.success else 'FAIL'} ({r.blocked} blocked, "
                                              f"{len(r.violations)} violations)")
        lines.append(f"| `{s}` | {first.track} | {first.measures} | " + " | ".join(cells) + " |")
    lines += ["", "| Agent | Task success | Blocked unsafe or invalid calls | Safety invariant violations |",
              "|---|---|---|---|"]
    for lab in labels:
        rs = [r for r in results if _label(r) == lab]
        lines.append(f"| {lab} | {sum(r.success for r in rs)}/{len(rs)} | {sum(r.blocked for r in rs)} | "
                     f"{sum(len(r.violations) for r in rs)} |")
    return "\n".join(lines) + "\n"


def write_results(results: list[RunResult], out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "results.md", out / "results.json"
    md.write_text(to_markdown(results), encoding="utf-8")
    js.write_text(json.dumps([asdict(r) for r in results], indent=1), encoding="utf-8")
    return md, js
