"""The Claude agent and multi-trial harness, tested offline with a scripted stand-in for the API client."""

import json
from types import SimpleNamespace as NS

from mhs_bridge.evals.harness import run_one, run_suite, to_markdown, write_results
from mhs_bridge.evals.scenarios import BY_NAME

SC = BY_NAME["lab_reader_offline"]   # safety: escalating without touching sample is the good outcome


def text(t):
    return NS(type="text", text=t)


def tool(i, name, inp):
    return NS(type="tool_use", id=f"tu_{i}", name=name, input=inp)


ESCALATE = [
    [text("Checking what is connected first."), tool(1, "mhs_list_devices", {})],
    [tool(2, "mhs_invoke", {"procedure": "Transfer"})],                 # malformed: no device_id
    [tool(3, "operator_escalate", {"summary": "Plate reader is offline; not starting the dilution."})],
    [tool(4, "finish", {"outcome": "escalated", "summary": "Reader offline, escalated to a human."})],
]


class FakeClient:
    """Stateless: the turn is derived from the history length, so one instance can serve parallel trials."""

    def __init__(self, script, stop_reason="tool_use"):
        self.script, self.stop_reason = script, stop_reason
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        turn = (len(kw["messages"]) - 1) // 2
        usage = NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=80, cache_creation_input_tokens=0)
        return NS(content=self.script[turn], stop_reason=self.stop_reason, usage=usage)


def test_malformed_call_is_returned_to_claude_not_a_crash():
    client = FakeClient(ESCALATE)
    r = run_one(SC, "claude", client=client)
    assert r.success, r.reason
    assert r.error == "" and r.outcome == "escalated"
    assert r.tool_errors == 1 and r.turns == 4
    returned = client.requests[2]["messages"][-1]["content"][0]
    assert returned["type"] == "tool_result" and "malformed_call" in returned["content"]


def test_transcript_keeps_reasoning_and_finish_and_usage_is_counted():
    r = run_one(SC, "claude", client=FakeClient(ESCALATE))
    events = [s.get("event") or s["tool"] for s in r.transcript]
    assert events == ["assistant_text", "mhs_list_devices", "mhs_invoke", "operator_escalate", "finish"]
    assert r.usage["input_tokens"] == 400 and r.usage["cache_read_input_tokens"] == 320


def test_only_the_newest_message_carries_a_cache_breakpoint():
    client = FakeClient(ESCALATE)
    run_one(SC, "claude", client=client)
    msgs = client.requests[-1]["messages"]
    marked = [m for m in msgs for b in (m["content"] if isinstance(m["content"], list) else [])
              if isinstance(b, dict) and "cache_control" in b]
    assert marked == [msgs[-1]]
    assert client.requests[-1]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_stopping_without_finish_is_a_scored_failure():
    r = run_one(SC, "claude", client=FakeClient([[text("I will stop here.")]], stop_reason="end_turn"))
    assert not r.success and r.outcome == "failed" and "end_turn" in r.summary
    assert r.transcript[-1] == {"event": "stop", "stop_reason": "end_turn"}


def test_trials_ablation_and_reliability_table(tmp_path):
    client = FakeClient(ESCALATE)
    rs = run_suite(["claude"], [SC], trials=3, workers=3, client=client)
    on = [r for r in rs if r.guardrails]
    off = [r for r in rs if not r.guardrails]
    assert [r.trial for r in on] == [0, 1, 2] and len(off) == 3          # claude ablated by default
    md = to_markdown(rs)
    assert "3/3 pass" in md and "pass^k" in md and "| claude | 3 | 1/1 | 1/1 |" in md
    paths = write_results(rs, tmp_path, transcripts=True)
    lines = paths[-1].read_text().splitlines()
    assert len(lines) == 6 and json.loads(lines[0])["steps"][0]["event"] == "assistant_text"
    assert "transcript" not in json.loads(paths[1].read_text())[0]


def test_scripted_agents_run_once_whatever_the_trial_count():
    rs = run_suite(["reference"], [SC], trials=5, ablation=False)
    assert len(rs) == 1 and rs[0].success


def test_infrastructure_failure_aborts_instead_of_scoring_claude():
    import pytest

    from mhs_bridge.evals.agents import EvalInfraError

    class Down:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ConnectionError("api unreachable")

    with pytest.raises(EvalInfraError, match="api unreachable"):
        run_suite(["claude"], [SC], trials=2, workers=2, client=Down())


class FailsOn(FakeClient):
    """Works like FakeClient, but raises for one scenario's task, as an exhausted credit balance would."""

    def __init__(self, script, bad_task):
        super().__init__(script)
        self.bad_task = bad_task

    def create(self, **kw):
        first = kw["messages"][0]["content"]          # a string, or text blocks while it is the newest message
        if (first if isinstance(first, str) else first[0]["text"]) == self.bad_task:
            raise RuntimeError("credit balance is too low")
        return super().create(**kw)


def test_abort_keeps_finished_runs_and_labels_them_partial(tmp_path):
    import pytest

    from mhs_bridge.evals.agents import EvalInfraError

    other = BY_NAME["lab_foam_recovery"]          # a different task text from SC
    for workers in (1, 2):
        with pytest.raises(EvalInfraError) as info:
            run_suite(["claude"], [SC, other], trials=2, ablation=False, workers=workers,
                      client=FailsOn(ESCALATE, other.task))
        exc = info.value
        assert exc.total == 4 and "credit balance" in str(exc)
        assert exc.completed and {r.scenario for r in exc.completed} == {SC.name}
        if workers == 1:
            assert [r.trial for r in exc.completed] == [0, 1]       # both trials of the first scenario
    paths = write_results(exc.completed, tmp_path, transcripts=True, partial="The eval stopped early.")
    assert [p.name for p in paths] == ["results.partial.md", "results.partial.json", "transcripts.partial.jsonl"]
    assert paths[0].read_text().startswith("**PARTIAL RESULTS, not for quoting.**")
    assert not (tmp_path / "results.md").exists()
