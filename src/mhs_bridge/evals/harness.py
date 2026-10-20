"""Eval harness: scenarios x agents, scored, with a guardrails-off ablation.

Metrics per run:
    success     the scenario's own definition of a good outcome, and no invariant violated
    blocked     calls refused by MHS checks, driver interlocks or application gates
    violations  independent invariant failures
    approvals   human approvals requested
    tool_calls  tool calls made

LLM agents are stochastic, so the suite can run several trials per scenario. Per scenario it reports
pass^k (every trial passed: the reliability a lab needs) and pass@k (at least one trial passed), plus
the per-run success rate. Each run's full transcript can be written to JSONL for review.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..environment import Environment, build
from ..mhs_shim import Decision
from .agents import AGENTS, EvalInfraError, SimulatedHuman, Toolbox
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
    trial: int = 0
    tool_errors: int = 0
    turns: int = 0
    usage: dict = field(default_factory=dict)
    transcript: list = field(default_factory=list, repr=False)

    def record(self) -> dict:
        """Everything except the transcript, which goes to its own file."""
        d = asdict(self)
        d.pop("transcript")
        return d


def run_one(sc: Scenario, agent_name: str, guardrails: bool = True, trial: int = 0,
            **agent_kwargs: Any) -> RunResult:
    env = build(sc.faults)
    for sr in sc.ehr_orders:
        env.clinical.receive_order(sr)
    if not guardrails:
        disable_guardrails(env)
    tb = Toolbox(env, SimulatedHuman(sc.human_approves))
    error = ""
    agent = None
    try:
        agent = AGENTS[agent_name](**agent_kwargs)
        report = agent.run(tb, sc)
    except EvalInfraError:
        raise
    except Exception as exc:  # noqa: BLE001 - an agent crash is a scored failure
        report, error = {"outcome": "failed", "summary": ""}, f"{type(exc).__name__}: {exc}"
    tb.human.tick(env)
    ok, reason = sc.check(env, report)
    offline = {d for d, f in sc.faults.items() if f.get("offline")}
    violations = check_invariants(env, sc.faults.get("mhs", {}).get("estop", False), offline)
    blocked = sum(e.outcome == "blocked" for e in env.runtime.audit if e.kind in ("invoke", "app"))
    return RunResult(sc.name, sc.track, sc.measures, agent_name, guardrails, ok and not violations and not error,
                     error or reason, report.get("outcome", ""), blocked, violations,
                     len(env.operator.approvals), tb.calls, report.get("summary", "")[:300], error, trial,
                     tb.tool_errors, getattr(agent, "turns", 0), dict(getattr(agent, "usage", {}) or {}),
                     tb.transcript)


DETERMINISTIC = {"naive", "reference"}


def run_suite(agents: list[str], scenarios: list[Scenario] | None = None, ablation: bool = True,
              trials: int = 1, ablation_agents: list[str] | None = None, workers: int = 1,
              **agent_kwargs: Any) -> list[RunResult]:
    """Run every agent on every scenario.

    trials           repeats per scenario for stochastic agents; scripted agents always run once
    ablation         also run the guardrails-off ablation
    ablation_agents  agents to ablate (default: naive, and claude when it is under test)
    workers          parallel runs; each run builds its own world, so runs are independent
    """
    scenarios = scenarios or SCENARIOS
    if ablation_agents is None:
        ablation_agents = [a for a in ("naive", "claude") if a in agents]
    jobs: list[tuple[Scenario, str, bool, int]] = []
    for guardrails in (True, False):
        if not guardrails and not ablation:
            break
        for name in (agents if guardrails else ablation_agents):
            n = 1 if name in DETERMINISTIC else max(1, trials)
            jobs += [(sc, name, guardrails, k) for sc in scenarios for k in range(n)]

    def go(job: tuple[Scenario, str, bool, int]) -> RunResult:
        sc, name, guardrails, k = job
        return run_one(sc, name, guardrails, k, **({} if name in DETERMINISTIC else agent_kwargs))

    if workers <= 1:
        return [go(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(go, jobs))   # map keeps job order, so output is stable


def _label(r: RunResult) -> str:
    return r.agent + ("" if r.guardrails else " (guardrails off)")


def _cell(rs: list[RunResult]) -> str:
    if len(rs) == 1:
        r = rs[0]
        return f"{'pass' if r.success else 'FAIL'} ({r.blocked} blocked, {len(r.violations)} violations)"
    k = sum(r.success for r in rs)
    return (f"{k}/{len(rs)} pass ({sum(r.blocked for r in rs) / len(rs):.1f} blocked/run, "
            f"{sum(len(r.violations) for r in rs)} violations)")


def to_markdown(results: list[RunResult]) -> str:
    labels = list(dict.fromkeys(_label(r) for r in results))
    by: dict[tuple[str, str], list[RunResult]] = {}
    for r in results:
        by.setdefault((r.scenario, _label(r)), []).append(r)
    lines = ["| Scenario | Track | Measures | " + " | ".join(labels) + " |",
             "|---|---|---|" + "|".join("---" for _ in labels) + "|"]
    for s in dict.fromkeys(r.scenario for r in results):
        first = next(r for r in results if r.scenario == s)
        cells = [_cell(by[(s, lab)]) if (s, lab) in by else "" for lab in labels]
        lines.append(f"| `{s}` | {first.track} | {first.measures} | " + " | ".join(cells) + " |")
    lines += ["", "| Agent | Task success | Blocked unsafe or invalid calls | Safety invariant violations |",
              "|---|---|---|---|"]
    multi = []
    for lab in labels:
        rs = [r for r in results if _label(r) == lab]
        lines.append(f"| {lab} | {sum(r.success for r in rs)}/{len(rs)} | {sum(r.blocked for r in rs)} | "
                     f"{sum(len(r.violations) for r in rs)} |")
        groups = [g for (_, label), g in by.items() if label == lab]
        if any(len(g) > 1 for g in groups):
            multi.append((lab, groups, rs))
    if multi:
        lines += ["", "| Agent | Trials | pass^k (all trials pass) | pass@k (any trial passes) | "
                      "Runs with a violation | Malformed calls | Mean turns |", "|---|---|---|---|---|---|---|"]
        for lab, groups, rs in multi:
            lines.append(f"| {lab} | {max(len(g) for g in groups)} | {sum(all(r.success for r in g) for g in groups)}"
                         f"/{len(groups)} | {sum(any(r.success for r in g) for g in groups)}/{len(groups)} | "
                         f"{sum(bool(r.violations) for r in rs)}/{len(rs)} | {sum(r.tool_errors for r in rs)} | "
                         f"{sum(r.turns for r in rs) / len(rs):.1f} |")
    return "\n".join(lines) + "\n"


def write_results(results: list[RunResult], out_dir: str | Path,
                  transcripts: bool = False) -> tuple[Path, ...]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "results.md", out / "results.json"
    md.write_text(to_markdown(results), encoding="utf-8")
    js.write_text(json.dumps([r.record() for r in results], indent=1), encoding="utf-8")
    if not transcripts:
        return md, js
    tr = out / "transcripts.jsonl"
    with tr.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({"scenario": r.scenario, "agent": r.agent, "guardrails": r.guardrails,
                                "trial": r.trial, "success": r.success, "reason": r.reason,
                                "violations": r.violations, "steps": r.transcript}, default=str) + "\n")
    return md, js, tr
