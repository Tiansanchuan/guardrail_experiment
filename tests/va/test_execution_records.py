"""Execution evidence at the real local runtime and frozen-store boundary."""

import asyncio
from datetime import date, datetime, time
from enum import Enum
import json

import pytest
from pydantic import BaseModel

from utils.functions_runtime import FunctionCall
from va_experiment.models import ModelReply, RunConfig
from va_experiment.model_client import ModelCallError, ModelCapabilityCallError
from va_experiment.execution import execute_task, execution_fingerprint
from va_experiment.records import RecordStore


def reply(*calls, finish_reason="tool_calls"):
    return ModelReply(
        message={"role": "assistant", "content": None, "tool_calls": list(calls)},
        raw_response={"provider": "raw", "requested": [c.model_dump() for c in calls]},
        finish_reason=finish_reason, usage={}, request_id="request-local",
    )


@pytest.mark.asyncio
async def test_ordered_calls_keep_mutation_before_failure(tmp_path, mini_task, scripted_client, valid_config):
    store = RecordStore(tmp_path)
    client = scripted_client([
        reply(
            FunctionCall(function="set_value", args={"key": "x", "value": 4}, id="first"),
            FunctionCall(function="change_then_fail", args={"key": "x", "value": 9}, id="second"),
        ), "finished",
    ])
    record = await execute_task(mini_task(), "normal_goal", client, RunConfig(**valid_config), store)
    first, second = [store.load_event(event_id) for event_id in record.event_ids]
    assert store.load_snapshot(first.before_snapshot_id) == {"values": {}}
    assert store.load_snapshot(first.after_snapshot_id) == {"values": {"x": 4}}
    assert store.load_snapshot(second.before_snapshot_id) == {"values": {"x": 4}}
    assert store.load_snapshot(second.after_snapshot_id) == {"values": {"x": 9}}
    assert second.executed and "fixture error after mutation" in second.error
    assert record.termination == "no_tool_calls"
    assert store.load_snapshot(record.final_snapshot_id) == {"values": {"x": 9}}


@pytest.mark.asyncio
async def test_end_task_closes_all_ids_and_skips_remaining_calls(tmp_path, mini_task, scripted_client, valid_config):
    client = scripted_client([reply(
        FunctionCall(function="set_value", args={"key": "x", "value": 4}, id="set"),
        FunctionCall(function="end_task", args={}, id="end"),
        FunctionCall(function="set_value", args={"key": "y", "value": 9}, id="skip"),
    )])
    store = RecordStore(tmp_path)
    record = await execute_task(mini_task(), "side_goal", client, RunConfig(**valid_config), store)
    assert record.termination == "end_task"
    assert store.load_snapshot(record.final_snapshot_id) == {"values": {"x": 4}}
    assert len(client.calls) == 1
    results = [m for m in record.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["set", "end", "skip"]
    assert json.loads(results[-1]["content"])["executed"] is False
    skipped = store.load_event(record.event_ids[-1])
    assert skipped.executed is False and "after end_task" in skipped.error
    assert skipped.before_snapshot_id == skipped.after_snapshot_id


@pytest.mark.asyncio
async def test_invalid_requests_have_no_effect_even_with_nested_calls(tmp_path, mini_task, scripted_client, valid_config):
    nested = FunctionCall(function="set_value", args={"key": "hidden", "value": 9})
    calls = [
        FunctionCall(function="missing", args={}, id="unknown"),
        FunctionCall(function="set_value", args={"key": "x", "value": "invalid"}, id="bad"),
        FunctionCall(function="set_value", args={"key": "x", "value": 1, "extra": nested}, id="nested"),
    ]
    store = RecordStore(tmp_path)
    record = await execute_task(mini_task(), "normal_goal", scripted_client([reply(*calls), "done"]), RunConfig(**valid_config), store)
    assert store.load_snapshot(record.final_snapshot_id) == {"values": {}}
    for event_id in record.event_ids:
        event = store.load_event(event_id)
        assert not event.executed and event.error
        assert event.before_snapshot_id == event.after_snapshot_id
    assert store.load_event(record.event_ids[-1]).args["extra"]["function"] == "set_value"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,termination", [
    ("context_limit", "context_limit"), ("length", "output_limit"),
    ("max_tokens", "output_limit"), ("content_filter", "refused"),
])
async def test_limited_reply_cannot_execute_synthetic_end_task(tmp_path, mini_task, scripted_client, valid_config, finish, termination):
    store = RecordStore(tmp_path)
    record = await execute_task(mini_task(), "normal_goal", scripted_client([
        reply(FunctionCall(function="end_task", args={}, id="synthetic"), finish_reason=finish),
    ]), RunConfig(**valid_config), store)
    assert record.termination == termination
    assert store.load_event(record.event_ids[0]).executed is False
    assert record.messages[-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_actor_budget_counts_replies_and_keeps_history_native_valid(tmp_path, mini_task, scripted_client, valid_config):
    valid_config["max_actor_turns"] = 1
    store = RecordStore(tmp_path)
    client = scripted_client([reply(
        FunctionCall(function="set_value", args={"key": "x", "value": 1}, id="one"),
        FunctionCall(function="set_value", args={"key": "y", "value": 2}, id="two"),
    )])
    record = await execute_task(mini_task(), "normal_goal", client, RunConfig(**valid_config), store)
    assert record.termination == "max_actor_turns"
    assert record.manifest["actor_turns"] == 1
    assert len(record.event_ids) == 2
    assert [m["tool_call_id"] for m in record.messages if m["role"] == "tool"] == ["one", "two"]


@pytest.mark.asyncio
async def test_raw_request_and_provider_response_are_durable_before_tool(tmp_path, mini_task, valid_config):
    store = RecordStore(tmp_path)
    task = mini_task()
    original_create = task.create_environment

    def checked_tool() -> str:
        """Inspect only the durable journal before returning."""
        rows = RecordStore(tmp_path).read_journal()
        assert [r["kind"] for r in rows][:3] == ["execution_started", "model_request", "model_response"]
        assert rows[2]["payload"]["reply"]["raw_response"] == {"preserved": "raw-reasoning"}
        assert rows[-1]["kind"] == "tool_started"
        return "observed"

    def create():
        environment = original_create()
        environment.add_tools([checked_tool])
        return environment

    task.create_environment = create

    class Client:
        async def complete(self, messages, runtime, *, purpose):
            if len(messages) > 2:
                return ModelReply(message={"role": "assistant", "content": "done", "tool_calls": None}, raw_response={}, finish_reason="stop", usage={}, request_id="end")
            messages[0]["content"] = "client mutation must not leak"
            return ModelReply(
                message={"role": "assistant", "content": None, "tool_calls": [FunctionCall(function="checked_tool", args={}, id="checked")]},
                raw_response={"preserved": "raw-reasoning"}, finish_reason="tool_calls", usage={}, request_id="checked",
            )

    record = await execute_task(task, "normal_goal", Client(), RunConfig(**valid_config), store)
    assert store.load_event(record.event_ids[0]).error is None
    assert "client mutation" not in record.messages[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,termination", [(ModelCallError, "model_error"), (ModelCapabilityCallError, "model_capability_error")])
async def test_failed_model_request_keeps_provider_attempt_journal(tmp_path, mini_task, scripted_client, valid_config, error_type, termination):
    raw = {"provider_response": {"unexpected": True}, "calls": [{"operation": "create", "error": {"type": "BadRequest", "message": "rejected"}}]}
    store = RecordStore(tmp_path)
    record = await execute_task(mini_task(), "normal_goal", scripted_client([error_type("failed", raw)]), RunConfig(**valid_config), store)
    assert record.termination == termination
    error_entry = next(r for r in RecordStore(tmp_path).read_journal() if r["kind"] == "model_error")
    assert error_entry["payload"]["raw_response"] == raw
    assert record.event_ids == []


@pytest.mark.asyncio
async def test_cancellation_is_frozen_and_propagated(tmp_path, mini_task, valid_config):
    class Client:
        async def complete(self, messages, runtime, *, purpose):
            raise asyncio.CancelledError("cancelled during model call")

    store = RecordStore(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await execute_task(mini_task(), "normal_goal", Client(), RunConfig(**valid_config), store)
    execution_id = store.read_journal()[0]["payload"]["execution_id"]
    assert RecordStore(tmp_path).load_execution(execution_id).termination == "cancelled"


@pytest.mark.asyncio
async def test_no_call_reply_ends_without_reminder_and_freeze_survives_reads(tmp_path, mini_task, scripted_client, valid_config):
    store = RecordStore(tmp_path)
    client = scripted_client(["done"])
    record = await execute_task(mini_task(), "normal_goal", client, RunConfig(**valid_config), store)
    assert len(client.calls) == 1 and len(record.messages) == 3
    assert record.termination == "no_tool_calls"
    path = tmp_path / "executions" / f"{record.execution_id}.json"
    frozen = path.read_bytes()
    initial = store.load_snapshot(record.initial_snapshot_id)
    initial["values"]["injected"] = 5
    record.messages[0]["content"] = "report branch mutation"
    reloaded = RecordStore(tmp_path).load_execution(record.execution_id)
    reloaded.messages.append({"role": "user", "content": "report request"})
    assert path.read_bytes() == frozen
    assert store.load_snapshot(record.initial_snapshot_id) == {"values": {}}
    pristine = store.load_execution(record.execution_id)
    assert pristine.fingerprint == execution_fingerprint(pristine)
    assert "branch mutation" not in pristine.messages[0]["content"]
    with pytest.raises(ValueError, match="Immutable"):
        store.save_execution(reloaded)


def test_snapshot_handles_pydantic_dates_enums_sets_and_function_calls(tmp_path):
    class Tag(Enum):
        A = "a"

    class Payload(BaseModel):
        date_value: date
        time_value: time
        datetime_value: datetime
        tags: set[str]
        tag: Tag
        call: FunctionCall

    payload = Payload(
        date_value=date(2024, 5, 15), time_value=time(8, 2), datetime_value=datetime(2024, 5, 15, 8, 2),
        tags={"z", "a"}, tag=Tag.A, call=FunctionCall(function="tool", args={"x": 1}, id="id"),
    )
    store = RecordStore(tmp_path)
    snapshot_id = store.save_snapshot(payload)
    assert store.load_snapshot(snapshot_id) == {
        "date_value": "2024-05-15", "time_value": "08:02:00", "datetime_value": "2024-05-15T08:02:00",
        "tags": ["a", "z"], "tag": "a", "call": {"function": "tool", "args": {"x": 1}, "id": "id", "placeholder_args": None},
    }
    payload.tags.add("later")
    assert store.load_snapshot(snapshot_id)["tags"] == ["a", "z"]
    assert store.save_snapshot(store.load_snapshot(snapshot_id)) == snapshot_id


def test_completed_events_survive_interrupted_journal(tmp_path):
    store = RecordStore(tmp_path)
    event = {
        "event_id": "completed", "call_id": "one", "tool_name": "set_value", "args": {"key": "x", "value": 4},
        "result": "set", "error": None, "before_snapshot_id": "before", "after_snapshot_id": "after", "message_index": 2, "executed": True,
    }
    store.append("tool_started", {"event_id": "completed"})
    store.append("tool_event", event)
    store.append("tool_started", {"event_id": "incomplete"})
    with store.journal_path.open("ab") as stream:
        stream.write(b'{"id":"torn')
    reloaded = RecordStore(tmp_path)
    assert reloaded.load_event("completed").result == "set"
    assert {d["kind"] for d in reloaded.diagnostics} == {"incomplete_journal", "incomplete_tool"}
    with pytest.raises(KeyError):
        reloaded.load_event("incomplete")
    with pytest.raises(ValueError, match="incomplete journal"):
        reloaded.append("anything", {})


def test_unfinished_execution_without_torn_bytes_is_diagnosed(tmp_path):
    store = RecordStore(tmp_path)
    store.append("execution_started", {"execution_id": "unfinished"})
    store.append("model_request", {"execution_id": "unfinished", "turn": 1, "messages": []})
    reloaded = RecordStore(tmp_path)
    assert any(d["kind"] == "incomplete_execution" for d in reloaded.diagnostics)
    with pytest.raises(FileNotFoundError):
        reloaded.load_execution("unfinished")


@pytest.mark.asyncio
async def test_unknown_provider_stop_is_never_natural_completion(tmp_path, mini_task, scripted_client, valid_config):
    store = RecordStore(tmp_path)
    record = await execute_task(mini_task(), "normal_goal", scripted_client([
        reply(finish_reason="unrecognized-provider-reason"),
    ]), RunConfig(**valid_config), store)
    assert record.termination == "unknown_finish_reason"


@pytest.mark.asyncio
async def test_serialization_loss_blocks_complete_trace(tmp_path, mini_task, scripted_client, valid_config):
    def opaque_result() -> str:
        """Return an unsupported runtime object for evidence diagnostics."""
        return object()

    task = mini_task()
    environment = task.create_environment()
    environment.add_tools([opaque_result])
    task.create_environment = lambda: environment
    store = RecordStore(tmp_path)
    record = await execute_task(task, "normal_goal", scripted_client([
        reply(FunctionCall(function="opaque_result", args={}, id="opaque")), "done",
    ]), RunConfig(**valid_config), store)
    assert record.manifest["complete_trace"] is False
    assert any(d["kind"] == "serialization_error" for d in record.manifest["diagnostics"])
    event = store.load_event(record.event_ids[0])
    assert event.executed is True
    assert event.result["__serialization_error__"]["type"] == "object"
    assert RecordStore(tmp_path).load_execution(record.execution_id).manifest["complete_trace"] is False


@pytest.mark.asyncio
async def test_cancellation_after_mutation_keeps_state_and_closes_skipped_ids(tmp_path, mini_task, scripted_client, valid_config):
    task = mini_task()
    environment = task.create_environment()

    async def mutate_and_cancel() -> str:
        """Mutate local state before asynchronous cancellation."""
        environment.values["changed"] = 1
        raise asyncio.CancelledError("cancelled after mutation")

    environment.add_tools([mutate_and_cancel])
    task.create_environment = lambda: environment
    store = RecordStore(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await execute_task(task, "normal_goal", scripted_client([reply(
            FunctionCall(function="mutate_and_cancel", args={}, id="cancel"),
            FunctionCall(function="set_value", args={"key": "skipped", "value": 1}, id="skip"),
        )]), RunConfig(**valid_config), store)
    execution_id = store.read_journal()[0]["payload"]["execution_id"]
    record = store.load_execution(execution_id)
    assert record.termination == "cancelled"
    assert store.load_snapshot(record.final_snapshot_id) == {"values": {"changed": 1}}
    first, second = [store.load_event(event_id) for event_id in record.event_ids]
    assert first.executed and "CancelledError" in first.error
    assert not second.executed
    assert [m["tool_call_id"] for m in record.messages if m["role"] == "tool"] == ["cancel", "skip"]


@pytest.mark.asyncio
async def test_goal_conditions_use_identical_neutral_tools(tmp_path, mini_task, scripted_client, valid_config):
    config = RunConfig(**valid_config)
    records = []
    for goal in ("normal_goal", "side_goal"):
        records.append(await execute_task(mini_task(), goal, scripted_client(["done"]), config, RecordStore(tmp_path / goal)))
    normal, side = records
    assert normal.tool_schema == side.tool_schema
    end_schema = next(t for t in normal.tool_schema if t["name"] == "end_task")
    assert end_schema["description"] == "End the current task execution."
    assert normal.messages[1] == side.messages[1]


@pytest.mark.asyncio
async def test_live_journal_read_does_not_mark_completed_trace_incomplete(tmp_path, mini_task, scripted_client, valid_config):
    store = RecordStore(tmp_path)

    def inspect_journal() -> str:
        """Read the live journal while execution is in progress."""
        store.read_journal()
        return "read"

    task = mini_task()
    environment = task.create_environment()
    environment.add_tools([inspect_journal])
    task.create_environment = lambda: environment
    record = await execute_task(task, "normal_goal", scripted_client([
        reply(FunctionCall(function="inspect_journal", args={}, id="read")), "done",
    ]), RunConfig(**valid_config), store)
    assert record.manifest["complete_trace"] is True
    assert RecordStore(tmp_path).diagnostics == []
