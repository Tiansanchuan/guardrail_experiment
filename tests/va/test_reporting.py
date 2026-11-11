"""Offline report branching at the frozen-history/client/store boundary."""

from copy import deepcopy
import asyncio
import random

import pytest

from utils.functions_runtime import FunctionCall
from va_experiment.models import ModelConfig, ModelReply
from va_experiment.model_client import ModelCallCancelledError, ModelCallError
from va_experiment.prompts import BASE_REPORT_REQUEST, VA_NOTICE
from va_experiment.records import RecordStore, execution_fingerprint
from va_experiment.reporting import generate_reports


@pytest.fixture
def frozen_run(tmp_path, stored_execution, valid_config):
    store = RecordStore(tmp_path)
    record = stored_execution("side_goal").record
    call = FunctionCall(function="set_value", args={"key": "x", "value": 4}, id="original-tool")
    record.messages.extend([
        {"role": "assistant", "content": "Executing the original task.", "tool_calls": [call]},
        {"role": "tool", "tool_call": call.model_copy(deep=True), "tool_call_id": call.id,
         "content": "set", "error": None},
        {"role": "assistant", "content": "Original execution summary.", "tool_calls": None},
    ])
    record.manifest["config"] = valid_config
    record.initial_snapshot_id = record.final_snapshot_id = store.save_snapshot({"values": {"x": 4}})
    store.save_execution(record)
    return record, store


@pytest.mark.asyncio
async def test_branches_share_complete_prefix_without_outputs_and_are_read_only(frozen_run, scripted_client):
    execution, store = frozen_run
    original = execution.model_copy(deep=True)
    persisted = (store.root / "executions" / f"{execution.execution_id}.json").read_bytes()
    client = scripted_client(["FIRST BRANCH ONLY", "Second branch."])
    client.config = ModelConfig(**execution.manifest["config"]["actor"])
    original_config = client.config.model_copy(deep=True)

    reports = await generate_reports(execution, client, store, order_seed=7)

    assert len(client.calls) == len(reports) == 2
    assert client.calls[0]["messages"][:-1] == client.calls[1]["messages"][:-1] == original.messages
    assert "Additional objective:\nSet y to 9." in client.calls[0]["messages"][0]["content"]
    assert "FIRST BRANCH ONLY" not in str(client.calls[1]["messages"])
    requests = {call["messages"][-1]["content"] for call in client.calls}
    assert requests == {BASE_REPORT_REQUEST, BASE_REPORT_REQUEST + "\n\n" + VA_NOTICE}
    assert all(call["runtime"].functions == {} for call in client.calls)
    assert client.config == original_config
    assert {r.protocol for r in reports} == {"ordinary", "va_notice"}
    assert all(r.status == "ok" and r.attempt_index == 0 for r in reports)
    assert len({r.prefix_fingerprint for r in reports}) == 1
    assert execution == original
    assert execution_fingerprint(execution) == original.fingerprint
    assert (store.root / "executions" / f"{execution.execution_id}.json").read_bytes() == persisted
    assert store.load_snapshot(execution.final_snapshot_id) == {"values": {"x": 4}}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    RuntimeError("local failure"),
    ModelCallError("native failure", {"provider_response": None, "calls": [
        {"request": {"model": "original-actor"}, "error": {"type": "ConnectionError"}},
    ]}),
])
async def test_first_branch_failure_is_durable_and_second_still_runs(frozen_run, scripted_client, failure):
    execution, store = frozen_run
    original_hash = execution.fingerprint
    client = scripted_client([failure, "Surviving branch"])
    reports = await generate_reports(execution, client, store, order_seed=7)
    assert len(client.calls) == len(reports) == 2
    first, second = reports
    assert first.status == "model_error" and first.reply is None
    assert first.error["type"] == type(failure).__name__
    assert first.error["raw_response"] == getattr(failure, "raw_response", None)
    assert second.status == "ok" and second.reply.message["content"] == "Surviving branch"
    journal = RecordStore(store.root).read_journal()
    errors = [r["payload"] for r in journal if r["kind"] == "report_error"]
    assert errors[0]["report_id"] == first.report_id
    assert errors[0]["error"] == first.error
    saved = [r["payload"] for r in journal if r["kind"] == "report_record"]
    assert [r["status"] for r in saved] == ["model_error", "ok"]
    assert execution_fingerprint(execution) == store.load_execution(execution.execution_id).fingerprint == original_hash


def model_reply(content="Public report.", *, reason="stop", calls=None, raw=None):
    return ModelReply(
        message={"role": "assistant", "content": content, "tool_calls": calls},
        raw_response=raw if raw is not None else {"provider_response": {"reasoning": "PRIVATE RAW"}},
        finish_reason=reason, usage={"output_tokens": 9}, request_id="native-report-request",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "status"), [
    (model_reply(""), "empty"),
    (model_reply("  \n"), "empty"),
    (model_reply(None), "empty"),
    (model_reply(reason="refusal"), "refused"),
    (model_reply(reason="content_filter"), "refused"),
    (model_reply(reason="max_tokens"), "truncated"),
    (model_reply(reason="length"), "truncated"),
    (model_reply(reason="context_limit"), "context_limit"),
    (model_reply(reason="error"), "model_error"),
    (model_reply(reason="future_unknown_stop"), "model_error"),
    (model_reply(reason="end_turn"), "ok"),
    (model_reply(reason="stop_sequence"), "ok"),
])
async def test_report_status_retains_provider_termination(frozen_run, scripted_client, response, status):
    execution, store = frozen_run
    reports = await generate_reports(execution, scripted_client([response, "Other report"]), store, order_seed=7)
    assert [r.status for r in reports] == [status, "ok"]
    assert reports[0].reply == response
    assert reports[0].reply.raw_response["provider_response"]["reasoning"] == "PRIVATE RAW"
    assert "PRIVATE RAW" not in (reports[0].reply.message["content"] or "")
    if status == "model_error":
        assert reports[0].error and response.finish_reason in str(reports[0].error)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "tool_calls", "tool_use"])
async def test_report_tool_request_executes_nothing(frozen_run, scripted_client, monkeypatch, reason):
    from utils.functions_runtime import FunctionsRuntime

    execution, store = frozen_run
    calls_executed = []

    def forbidden_execution(*args, **kwargs):
        calls_executed.append((args, kwargs))
        raise AssertionError("No tools may run during reporting")

    monkeypatch.setattr(FunctionsRuntime, "run_function", forbidden_execution)
    call = FunctionCall(function="set_value", args={"key": "x", "value": 999}, id="forbidden")
    reply = model_reply(calls=[call], reason=reason)
    client = scripted_client([reply, "Other branch"])
    reports = await generate_reports(execution, client, store, order_seed=7)
    assert [r.status for r in reports] == ["tool_request", "ok"]
    assert reports[0].reply == reply
    assert calls_executed == []
    assert len(client.calls) == 2
    assert not any(row["kind"] == "tool_event" for row in store.read_journal())
    assert store.load_snapshot(execution.final_snapshot_id) == {"values": {"x": 4}}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_branch", [0, 1])
async def test_cancellation_keeps_native_request_evidence_and_propagates(frozen_run, cancel_branch):
    execution, store = frozen_run
    raw = {"provider_response": None, "calls": [
        {"operation": "messages.create", "request": {"model": "original-actor"},
         "response": None, "error": {"type": "CancelledError", "message": "cancelled"}},
    ]}
    failure = ModelCallCancelledError("cancelled", raw)

    class CancellingClient:
        calls = 0

        async def complete(self, messages, runtime, *, purpose):
            index = self.calls
            self.calls += 1
            if index == cancel_branch:
                raise failure
            return model_reply("Completed first branch")

    client = CancellingClient()
    with pytest.raises(asyncio.CancelledError) as raised:
        await generate_reports(execution, client, store, order_seed=7)
    assert raised.value is failure
    assert client.calls == cancel_branch + 1
    rows = RecordStore(store.root).read_journal()
    errors = [r["payload"]["error"] for r in rows if r["kind"] == "report_error"]
    assert errors == [{"type": "ModelCallCancelledError", "message": "cancelled", "raw_response": raw}]
    records = [r["payload"] for r in rows if r["kind"] == "report_record"]
    assert len(records) == cancel_branch + 1
    assert records[-1]["status"] == "model_error" and records[-1]["reply"] is None
    assert records[-1]["error"] == errors[0]
    assert rows[-1]["kind"] == "report_pair_cancelled"
    assert rows[-1]["payload"]["report_ids"] == [r["report_id"] for r in records]
    assert store.load_execution(execution.execution_id).fingerprint == execution.fingerprint


@pytest.mark.asyncio
async def test_explicit_reruns_keep_failures_and_select_first_valid_chronologically(frozen_run, scripted_client):
    from va_experiment.reporting import load_reports, select_first_valid_report

    execution, store = frozen_run
    first = await generate_reports(execution, scripted_client([RuntimeError("first failure"), "First VA"]), store, order_seed=7)
    second = await generate_reports(execution, scripted_client(["First ordinary", "Later VA"]), store, order_seed=7)
    third = await generate_reports(execution, scripted_client(["Later ordinary", "Last VA"]), store, order_seed=7)
    saved = load_reports(RecordStore(store.root), execution.execution_id)
    assert saved == first + second + third
    assert [r.attempt_index for r in saved] == [0, 0, 1, 1, 2, 2]
    assert saved[0].status == "model_error" and saved[0].error["message"] == "first failure"
    assert select_first_valid_report(saved, "ordinary") == second[0]
    assert select_first_valid_report(saved, "va_notice") == first[1]
    assert select_first_valid_report(first, "ordinary") is None
    assert load_reports(store, "unrelated-execution") == []


@pytest.mark.asyncio
async def test_provider_mutation_cannot_change_other_branch_or_frozen_execution(frozen_run):
    execution, store = frozen_run
    original = execution.model_copy(deep=True)

    class MutatingClient:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, runtime, *, purpose):
            self.calls.append({"messages": deepcopy(messages), "functions": dict(runtime.functions)})
            messages[0]["content"] = "Mutated system"
            messages[2]["tool_calls"][0].args["value"] = 999
            messages[3]["content"] = "Mutated tool result"
            messages.append({"role": "user", "content": "Leaked first branch"})
            runtime.functions["unexpected"] = object()
            return model_reply()

    client = MutatingClient()
    await generate_reports(execution, client, store, order_seed=7)
    assert len(client.calls) == 2
    assert all(c["messages"][:-1] == original.messages and c["functions"] == {} for c in client.calls)
    assert execution == store.load_execution(execution.execution_id) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["context_limit", "max_actor_turns", "model_error", "output_limit"])
async def test_partial_execution_stays_diagnostic_but_report_uses_own_stop(frozen_run, scripted_client, termination):
    original, store = frozen_run
    execution = original.model_copy(deep=True)
    execution.execution_id += "-partial"
    execution.termination = termination
    store.save_execution(execution)
    client = scripted_client([model_reply("What I managed to do."), model_reply(reason="context_limit")])
    reports = await generate_reports(execution, client, store, order_seed=7)
    assert [r.status for r in reports] == ["ok", "context_limit"]
    assert len(client.calls) == 2
    assert all(c["messages"][:-1] == execution.messages for c in client.calls)
    pair = next(r["payload"] for r in store.read_journal() if r["kind"] == "report_pair_started")
    assert pair["source_termination"] == termination
    assert store.load_execution(execution.execution_id).termination == termination


@pytest.mark.asyncio
@pytest.mark.parametrize(("seed", "order"), [
    (7, ["ordinary", "va_notice"]), (1, ["va_notice", "ordinary"]),
])
async def test_seed_controls_only_local_scheduling_and_is_journalled(frozen_run, scripted_client, seed, order):
    execution, store = frozen_run
    rng_state = random.getstate()
    reports = await generate_reports(execution, scripted_client(["One", "Two"]), store, order_seed=seed)
    assert [r.protocol for r in reports] == order
    assert random.getstate() == rng_state
    started = next(r["payload"] for r in store.read_journal() if r["kind"] == "report_pair_started")
    assert started["order_seed"] == seed and started["protocol_order"] == order
    assert started["inference_rng_support"] == "unknown"
    assert started["actor_config"] == execution.manifest["config"]["actor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["input", "snapshot_before", "snapshot_after"])
async def test_frozen_evidence_is_verified_before_and_after_reporting(frozen_run, scripted_client, corruption):
    execution, store = frozen_run
    snapshot_path = store.root / "snapshots" / f"{execution.final_snapshot_id}.json"
    if corruption == "input":
        execution.messages[0]["content"] = "Uncommitted replacement system"
    elif corruption == "snapshot_before":
        snapshot_path.write_text("{}", encoding="utf-8")
    client = scripted_client(["One", "Two"])
    complete = client.complete

    async def mutate_after_request(messages, runtime, *, purpose):
        reply = await complete(messages, runtime, purpose=purpose)
        if corruption == "snapshot_after":
            snapshot_path.write_text("{}", encoding="utf-8")
        return reply

    client.complete = mutate_after_request
    with pytest.raises(ValueError, match="committed execution|Snapshot fingerprint"):
        await generate_reports(execution, client, store, order_seed=7)
    assert len(client.calls) == (2 if corruption == "snapshot_after" else 0)
    assert not any(r["kind"] == "report_pair_finished" for r in store.read_journal())


@pytest.mark.asyncio
async def test_native_structured_refusal_is_not_mistaken_for_empty_report(frozen_run, scripted_client):
    execution, store = frozen_run
    response = model_reply(None, raw={"provider_response": {"choices": [
        {"message": {"refusal": "Cannot provide this report", "content": None}},
    ]}, "calls": []})
    reports = await generate_reports(execution, scripted_client([response, "Other branch"]), store, order_seed=7)
    assert reports[0].status == "refused"
    assert reports[0].reply == response


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "replacement"), [
    ("provider", "openai"), ("model_id", "different-model"),
    ("temperature", 0.8), ("max_output_tokens", 999),
])
async def test_configured_report_client_must_match_original_actor(frozen_run, scripted_client, field, replacement):
    execution, store = frozen_run
    client = scripted_client(["One", "Two"])
    settings = deepcopy(execution.manifest["config"]["actor"])
    settings[field] = replacement
    if settings["provider"] != "fake":
        settings["api_key_env"] = "OFFLINE_TEST_KEY"
    client.config = ModelConfig(**settings)
    with pytest.raises(ValueError, match="actor configuration"):
        await generate_reports(execution, client, store, order_seed=7)
    assert client.calls == [] and store.read_journal() == []


@pytest.mark.asyncio
async def test_explicit_rerun_does_not_reuse_an_interrupted_request_attempt(frozen_run, scripted_client):
    execution, store = frozen_run
    store.append("report_request", {
        "pair_id": "interrupted-pair", "report_id": "interrupted-report",
        "execution_id": execution.execution_id, "protocol": "ordinary", "attempt_index": 0,
    })
    reports = await generate_reports(execution, scripted_client(["Ordinary", "VA"]), store, order_seed=7)
    assert [r.attempt_index for r in reports] == [1, 0]
    requests = [r["payload"] for r in store.read_journal() if r["kind"] == "report_request"]
    assert requests[0]["report_id"] == "interrupted-report"
    assert [(r["protocol"], r["attempt_index"]) for r in requests] == [
        ("ordinary", 0), ("ordinary", 1), ("va_notice", 0),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "expected"), [
    ("context_limit", "context_limit"), ("max_tokens", "truncated"),
    ("length", "truncated"), ("unknown_stop", "model_error"),
])
async def test_premature_stop_with_end_task_request_cannot_be_success(frozen_run, scripted_client, reason, expected):
    execution, store = frozen_run
    synthetic_end = FunctionCall(function="end_task", args={}, id="synthetic-end")
    response = model_reply("Incomplete response", reason=reason, calls=[synthetic_end])
    reports = await generate_reports(execution, scripted_client([response, "Other report"]), store, order_seed=7)
    assert reports[0].status == expected
    assert reports[0].reply == response
    assert not any(row["kind"].startswith("tool_") for row in store.read_journal())
