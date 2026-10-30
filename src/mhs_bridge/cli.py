"""mhs-bridge: discovery, demos, evals, the MCP server, and the human and EHR channels."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .environment import build
from .evals.agents import EvalInfraError, ReferenceAgent, SimulatedHuman, Toolbox
from .evals.harness import run_suite, to_markdown, write_results
from .evals.scenarios import BY_NAME, SCENARIOS
from .protocols.sila2 import parse_feature

DEMOS = {"research": ["lab_foam_recovery", "lab_saturation_rerun"],
         "clinical": ["order_to_result_astm", "order_missing_accession", "order_patient_mismatch", "clin_critical_ack"],
         "bedside": ["vitals_chart", "vitals_artefact"]}


def _tail(t: dict) -> str:
    out = t["output"]
    if t["tool"] in ("mhs_describe_device", "app_describe"):
        return "reference file"
    if isinstance(out, dict):
        if "saturated_wells" in out:
            return (f"R2={out.get('r2')} top={out.get('top_conc_ug_ml')} ug/mL saturated={out['saturated_wells']} "
                    f"invalid={out['invalid_wells']}")
        if out.get("status") in ("blocked", "error"):
            return f"{out['status']} {out.get('rule') or out.get('error')}: {out.get('reason') or out.get('detail', '')}"
        if "status" in out:
            return out["status"] + (f" {out['request_id']}" if out.get("request_id") else "") + (
                f" ({out['kind']}, sha256 {out['payload_sha256'][:12]})" if out.get("payload_sha256") else "")
    if isinstance(out, list) and out and isinstance(out[0], dict) and "release" in out[0]:
        return "; ".join(f"{r['test_display'].split(' [')[0]} {r['value']} {r['unit']} {r['release']}"
                         + (f" {r['hold_reasons']}" if r["hold_reasons"] else "") for r in out)
    return json.dumps(out, default=str)


def _head(t: dict) -> str:
    i = t["input"]
    if t["tool"] in ("mhs_invoke", "app_invoke"):
        target = i.get("device_id") or i.get("app_id")
        return f"{t['tool']} {target}.{i['procedure']}({json.dumps(i['args'])})"
    return f"{t['tool']}({json.dumps(i)})"


def cmd_demo(track: str) -> int:
    for name in DEMOS[track]:
        sc = BY_NAME[name]
        env = build(sc.faults)
        print(f"\n=== {name}: {sc.summary}")
        for sr in sc.ehr_orders:
            r = env.clinical.receive_order(sr)
            print(f"EHR -> ServiceRequest/{r['order_id']} routed {r['routing']}")
        tb = Toolbox(env, SimulatedHuman(sc.human_denies))
        report = ReferenceAgent().run(tb, sc)
        print()
        for n, t in enumerate(tb.transcript, 1):
            print(f"{n:>3}. {_head(t)[:118]}\n       -> {_tail(t)[:150]}")
        print(f"\noutcome: {report['outcome']}: {report['summary']}")
        if env.ehr.bundles:
            print("FHIR to the EHR:")
            for b in env.ehr.bundles:
                for e in b["entry"]:
                    res = e["resource"]
                    rt = res["resourceType"]
                    if rt == "Observation":
                        q = res.get("valueQuantity", {})
                        based = f" basedOn {res['basedOn'][0]['reference']}" if res.get("basedOn") else ""
                        print(f"  Observation {res['code']['coding'][0]['code']} = {q.get('value')} {q.get('unit')} "
                              f"[{res['category'][0]['coding'][0]['code']}]{based}")
                    elif rt == "Task":
                        why = f" ({res['statusReason']['text']})" if res.get("statusReason") else ""
                        print(f"  Task {res['id']} {res['status']}{why}")
                    elif rt == "DiagnosticReport":
                        print(f"  DiagnosticReport {res['id']} basedOn {[x['reference'] for x in res.get('basedOn', [])]}")
                    elif rt == "Communication":
                        print(f"  Communication {res['id']} to {res['recipient'][0]['display']}")
    return 0


def cmd_sila(path: str) -> int:
    """Show the MHS procedures a SiLA 2 Feature Definition would produce."""
    from .drivers.sila2 import SiLA2Driver
    feature = parse_feature(Path(path).read_text(encoding="utf-8"))
    profile = {"device_id": "preview", "device_type": "instrument", "vendor": feature.originator,
               "model": feature.identifier}
    print(SiLA2Driver(profile, client=None, features=[feature]).manifest().reference())
    return 0


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_sha() -> str:
    import os
    import subprocess
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).resolve().parent).stdout.strip()
    except Exception:  # noqa: BLE001 - provenance is best effort outside a checkout
        return ""


def write_meta(out: str, **run: object) -> Path:
    """Provenance for a results folder: what ran, on which code, with which SDK. Published numbers need it."""
    import platform
    from importlib import metadata
    try:
        sdk = metadata.version("anthropic")
    except metadata.PackageNotFoundError:
        sdk = ""
    meta = {"package_version": __version__, "git_sha": _git_sha(), "anthropic_sdk": sdk,
            "python": platform.python_version(), **run, "finished": _now()}
    if "claude" in run.get("agents", []):
        import os
        meta["model"] = run.get("model") or os.environ.get("MHS_CLAUDE_MODEL", "claude-sonnet-5-5")
    path = Path(out) / "meta.json"
    path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return path


def _load(state_dir: str, name: str, default: object) -> object:
    p = Path(state_dir) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _chain_banner(state_dir: str) -> str:
    st = _load(state_dir, "review_status.json", {})
    if st.get("chain") == "broken":
        return f"REVIEW LOG BROKEN: {st['problem']}. The server applies no further decisions.\n"
    return ""


def cmd_show(state_dir: str, request_id: str) -> int:
    """One draft for review: what was asked for beside what would be sent, then the payload and the exceptions."""
    d = next((x for x in _load(state_dir, "drafts.json", []) if x["request_id"] == request_id), None)
    if d is None:
        print(f"no draft {request_id} in {state_dir}/drafts.json", file=sys.stderr)
        return 2
    t, src = d["target"], d["intent"].get("source", {})
    print(_chain_banner(state_dir) + f"{d['request_id']}  rev {d['revision']}  {d['kind']}  {d['status']}  "
          f"{t['app']}.{t['procedure']}  sha256 {d['payload']['sha256']}")
    print(f"requested by {d['requested_by']} at {d['created_at']}"
          + (f"; supersedes {d['supersedes']}" if d["supersedes"] else ""))
    print(f"\nINTENT  {src.get('type')} {src.get('id', '')}\n  {src.get('text', '')}")
    if d["intent"].get("agent_rationale"):
        print(f"  agent: {d['intent']['agent_rationale']}")
    print("\nPROPOSED MAPPING")
    width = max([len(f["path"]) for f in d["proposed"]["fields"]] + [4])
    for f in d["proposed"]["fields"]:
        mark = " *" if f.get("changed_in_revision") else ""
        print(f"  {f['path']:<{width}}  {str(f['value']):<24}  <- {f['from']}{mark}")
    for e in d["exceptions"]:
        print(f"\nEXCEPTION {e['exception_id']}: {e['issue']} on {e['field']}\n  {e['detail']}")
        for s in e["suggestions"]:
            tag = "plausible" if s["plausible"] else "implausible"
            print(f"  - {s['value']!r} [{tag}, unverified]: {s['basis']} ({'; '.join(s['evidence'])})")
    print(f"\nPAYLOAD ({d['payload'].get('format')})\n{d['payload'].get('text', '')}")
    print(f"\ngates: {d['gates']}")
    for h in d["history"]:
        print(f"  {h['at']}  {h['status']:<17} {h['by']}  {h['reason']}")
    return 0


def _decide(a: argparse.Namespace, record: dict) -> int:
    if not a.operator:
        print("--as is required: decisions are attributed to a named human", file=sys.stderr)
        return 2
    d = Path(a.state_dir)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "decisions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**record, "approver": f"human:{a.operator}", **({"note": a.note} if getattr(a, "note", "")
                                                                             else {})}) + "\n")
    print(f"recorded; the server applies it on its next tool call: {record}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mhs-bridge", description=__doc__)
    ap.add_argument("--version", action="version", version=f"mhs-interop-bridge {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="discover devices")
    d = sub.add_parser("describe", help="print a device reference file")
    d.add_argument("device_id")
    s = sub.add_parser("sila-preview", help="show the MHS manifest generated from a SiLA 2 Feature Definition")
    s.add_argument("fdl")
    sub.add_parser("scenarios", help="list eval scenarios")
    dm = sub.add_parser("demo", help="watch the reference agent")
    dm.add_argument("track", choices=list(DEMOS))
    ev = sub.add_parser("eval", help="run the eval suite")
    ev.add_argument("--agents", default="naive,reference")
    ev.add_argument("--scenario", action="append", help="scenario name; repeat it or give a comma-separated list")
    ev.add_argument("--model")
    ev.add_argument("--out")
    ev.add_argument("--no-ablation", action="store_true")
    ev.add_argument("--trials", type=int, default=1, help="runs per scenario for LLM agents (scripted agents run once)")
    ev.add_argument("--ablate", help="comma-separated agents to run with guardrails off (default: naive, and claude "
                                     "when it is under test)")
    ev.add_argument("--workers", type=int, default=1, help="parallel runs")
    ev.add_argument("--transcripts", action="store_true",
                    help="write transcripts.jsonl next to the results (on by default when claude is an agent)")
    ev.add_argument("-v", "--verbose", action="store_true")
    ev.add_argument("--mode", choices=["autonomous", "copilot"], default="autonomous",
                    help="copilot: every device invoke is drafted for the simulated human's review")
    ev.add_argument("--knowledge", choices=["on", "off"], default="on",
                    help="off: device profiles without tags and error hints, and no physical-failure line in the "
                         "Claude prompt, to measure what the agent infers")
    sv = sub.add_parser("serve", help="run the MCP server over stdio")
    sv.add_argument("--scenario")
    sv.add_argument("--mode", choices=["autonomous", "copilot"], default="autonomous",
                    help="copilot: every device invoke is drafted for a human's review as well as clinical messages")
    sv.add_argument("--state-dir", default=".mhs_state")
    sv.add_argument("--audit")
    so = sub.add_parser("submit-order", help="EHR channel: drop a FHIR ServiceRequest into a running server")
    so.add_argument("file")
    so.add_argument("--state-dir", default=".mhs_state")
    for name in ("approvals", "drafts", "show", "approve", "deny", "revise", "clear-estop"):
        p = sub.add_parser(name, help=f"human channel: {name}")
        p.add_argument("--state-dir", default=".mhs_state")
        if name in ("show", "approve", "deny", "revise"):
            p.add_argument("request_id")
        if name in ("approve", "deny", "revise", "clear-estop"):
            p.add_argument("--as", dest="operator")
        if name in ("approve", "deny", "revise"):
            p.add_argument("--note", default="")
        if name == "revise":
            p.add_argument("--set", action="append", default=[], metavar="ARG=VALUE",
                           help="change one argument of the draft; repeatable. VALUE is JSON if it parses, else text")

    a = ap.parse_args(argv)
    if a.cmd == "devices":
        for dev in build().runtime.discover():
            print(json.dumps(dev))
    elif a.cmd == "describe":
        print(build().runtime.describe(a.device_id))
    elif a.cmd == "sila-preview":
        return cmd_sila(a.fdl)
    elif a.cmd == "scenarios":
        for sc in SCENARIOS:
            print(f"{sc.name:<26} {sc.track:<9} {sc.measures:<11} {sc.summary}")
    elif a.cmd == "demo":
        return cmd_demo(a.track)
    elif a.cmd == "eval":
        agents = [x.strip() for x in a.agents.split(",") if x.strip()]
        names = [n.strip() for arg in (a.scenario or []) for n in arg.split(",") if n.strip()]
        unknown = [n for n in names if n not in BY_NAME]
        if unknown:
            print(f"unknown scenario {', '.join(unknown)}; run `mhs-bridge scenarios` for the list", file=sys.stderr)
            return 2
        scenarios = [BY_NAME[n] for n in dict.fromkeys(names)] if names else SCENARIOS
        ablate = [x.strip() for x in a.ablate.split(",") if x.strip()] if a.ablate else None
        transcripts = a.transcripts or "claude" in agents
        progress = None
        if a.out:
            Path(a.out).mkdir(parents=True, exist_ok=True)
            progress = Path(a.out) / "progress.jsonl"
            progress.write_text("", encoding="utf-8")

        def log_run(r) -> None:   # one line per finished run, so even a killed job leaves a record
            if progress:
                with progress.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(r.record(), default=str) + "\n")

        started = _now()
        try:
            results = run_suite(agents, scenarios, ablation=not a.no_ablation, trials=a.trials, ablation_agents=ablate,
                                workers=a.workers, on_result=log_run, mode=a.mode, knowledge=a.knowledge,
                                **({"model": a.model} if a.model else {}))
        except EvalInfraError as exc:
            kept = f"{len(exc.completed)} of {exc.total} runs finished before it"
            if a.out and exc.completed:
                paths = write_results(exc.completed, a.out, transcripts=transcripts,
                                      partial=f"The eval stopped early: {exc}. {kept}.")
                print("wrote", *paths, file=sys.stderr)
            print(f"eval aborted, no complete results written ({kept}): {exc}", file=sys.stderr)
            return 2
        print(to_markdown(results))
        if a.out:
            written = list(write_results(results, a.out, transcripts=transcripts))
            if "claude" in agents:   # scripted results are deterministic and committed; LLM runs need provenance
                written.append(write_meta(a.out, agents=agents, model=a.model, trials=a.trials, runs=len(results),
                                          scenarios=[s.name for s in scenarios], ablation=not a.no_ablation,
                                          mode=a.mode, knowledge=a.knowledge, started=started))
            print("wrote", *written)
            progress.unlink(missing_ok=True)
        if a.verbose:
            for r in results:
                print(f"- {r.agent}{'' if r.guardrails else ' (off)'} {r.scenario}: {r.reason}; {r.violations[:2]}")
    elif a.cmd == "serve":
        from .mcp_server import main as serve
        serve(a.scenario, a.state_dir, a.audit, a.mode)
    elif a.cmd == "submit-order":
        sr = json.loads(Path(a.file).read_text())
        inbox = Path(a.state_dir) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{sr['id']}.json").write_text(json.dumps(sr), encoding="utf-8")
        print(f"ServiceRequest/{sr['id']} queued for the clinical bridge")
    elif a.cmd in ("approvals", "drafts"):
        rows = _load(a.state_dir, "pending.json" if a.cmd == "approvals" else "drafts.json", [])
        print(_chain_banner(a.state_dir) + ("\n".join(
            f"{r['request_id']}  {r['status']:<17} {r['kind']:<17} {r['target']['app']}.{r['target']['procedure']}  "
            f"{json.dumps(r['target']['args'])}" + (f"  [{len(r['exceptions'])} exception(s)]" if r["exceptions"] else "")
            for r in rows) or ("no pending approvals" if a.cmd == "approvals" else "no drafts")))
        if rows and a.cmd == "approvals":
            print("\nmhs-bridge show <id> to review one; approve, deny or revise it --as <your name>")
    elif a.cmd == "show":
        return cmd_show(a.state_dir, a.request_id)
    elif a.cmd in ("approve", "deny"):
        return _decide(a, {"request_id": a.request_id, "approve": a.cmd == "approve"})
    elif a.cmd == "revise":
        changes = {}
        for item in a.set:
            key, sep, raw = item.partition("=")
            if not sep:
                print(f"--set {item!r}: expected ARG=VALUE", file=sys.stderr)
                return 2
            try:
                changes[key] = json.loads(raw)
            except json.JSONDecodeError:
                changes[key] = raw
        if not changes:
            print("revise needs at least one --set ARG=VALUE", file=sys.stderr)
            return 2
        return _decide(a, {"type": "revise", "request_id": a.request_id, "set": changes})
    elif a.cmd == "clear-estop":
        return _decide(a, {"type": "clear_estop"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
