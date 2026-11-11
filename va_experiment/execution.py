"""Independent actor loop: execute tools once, journal evidence, freeze history."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import json
from uuid import uuid4

from utils.functions_runtime import FunctionCall, FunctionsRuntime, make_function
from va_experiment.models import ExecutionRecord, ModelCapabilityError, ModelClient, RunConfig, TaskAdapter, ToolEvent
from va_experiment.prompts import actor_messages, neutral_end_task
from va_experiment.records import RecordStore, execution_fingerprint


def _nested_call(value) -> bool:
    if isinstance(value, FunctionCall):
        return True
    if isinstance(value, dict):
        return any(_nested_call(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_nested_call(child) for child in value)
    return False


def _error(exc: BaseException) -> dict:
    return {"type": type(exc).__name__, "message": str(exc)}


async def execute_task(
    adapter: TaskAdapter, goal: str, client: ModelClient, config: RunConfig, store: RecordStore,
) -> ExecutionRecord:
    if goal not in {"normal_goal", "side_goal"}:
        raise ValueError(f"Invalid goal condition: {goal}")
    environment = adapter.create_environment()
    runtime = FunctionsRuntime()
    for tool in environment.available_tools:
        function = make_function(tool)
        if function.name != "end_task":
            runtime.register_function(function)
    runtime.register_function(make_function(neutral_end_task))
    schema = [{"name": f.name, "description": f.description, "parameters": f.parameters.model_json_schema()}
              for f in runtime.functions.values()]
    messages = actor_messages(adapter.normal_task, adapter.clean_side_goal if goal == "side_goal" else None)
    execution_id = uuid4().hex
    serialization_start = sum(d["kind"] == "serialization_error" for d in store.diagnostics)
    manifest_config = store.serialize(config)
    manifest_config["output_dir"] = str(config.output_dir.resolve())
    initial = store.save_snapshot(environment)
    store.append("execution_started", {
        "execution_id": execution_id, "task_id": adapter.task_id, "goal_condition": goal,
        "initial_snapshot_id": initial, "messages": messages, "tool_schema": schema,
        "config": manifest_config,
    })
    event_ids = []
    termination = "max_actor_turns"
    turns = 0
    cancellation = None
    execution_error = None
    tool_errors = []
    try:
        for turn in range(config.max_actor_turns):
            store.append("model_request", {"execution_id": execution_id, "turn": turn + 1, "messages": messages})
            try:
                response = await client.complete(deepcopy(messages), runtime, purpose="actor_execution")
            except asyncio.CancelledError as exc:
                cancellation = exc
                termination = "cancelled"
                store.append("model_error", {
                    "execution_id": execution_id, "turn": turn + 1, "error": _error(exc),
                    "raw_response": getattr(exc, "raw_response", None),
                })
                break
            except Exception as exc:
                termination = "model_capability_error" if isinstance(exc, ModelCapabilityError) else "model_error"
                execution_error = _error(exc)
                store.append("model_error", {
                    "execution_id": execution_id, "turn": turn + 1, "error": execution_error,
                    "raw_response": getattr(exc, "raw_response", None),
                })
                break
            turns += 1
            store.append("model_response", {"execution_id": execution_id, "turn": turn + 1, "reply": response})
            message_index = len(messages)
            messages.append(deepcopy(response.message))
            reason = response.finish_reason
            blocked_reason = {
                "context_limit": "context_limit", "length": "output_limit", "max_tokens": "output_limit",
                "refusal": "refused", "content_filter": "refused", "error": "model_error",
            }.get(reason)
            if not blocked_reason and reason not in {"stop", "end_turn", "stop_sequence", "tool_calls", "tool_use"}:
                blocked_reason = "unknown_finish_reason"
            if blocked_reason:
                termination = blocked_reason
            calls = response.message.get("tool_calls") or []
            ended = False
            for call in calls:
                event_id = uuid4().hex
                before = store.save_snapshot(environment)
                raw_args = store.serialize(call.args)
                store.append("tool_started", {
                    "execution_id": execution_id, "event_id": event_id, "call_id": call.id,
                    "tool_name": call.function, "args": raw_args, "before_snapshot_id": before,
                })
                result, error, executed = None, None, False
                try:
                    if ended or blocked_reason or cancellation:
                        raise ValueError("Not executed: " + ("after end_task" if ended else termination))
                    if call.function not in runtime.functions:
                        raise ValueError(f"Unknown tool: {call.function}")
                    function = runtime.functions[call.function]
                    if _nested_call(call.args):
                        raise ValueError("Nested FunctionCall arguments are not executable journaled requests")
                    extras = set(call.args) - set(function.parameters.model_fields)
                    if extras:
                        raise ValueError(f"Unexpected tool arguments: {sorted(extras)}")
                    function.parameters.model_validate(call.args)
                    executed = True
                    result, _ = runtime.run_function(environment, call.function, deepcopy(call.args), raise_on_error=True)
                    if inspect.isawaitable(result):
                        result = await result
                    if call.function == "end_task":
                        ended = True
                        termination = "end_task"
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    termination = "cancelled"
                    error = _error(exc)
                except Exception as exc:
                    error = _error(exc)
                after = store.save_snapshot(environment)
                result = store.serialize(result)
                error_text = json.dumps(error, ensure_ascii=False, sort_keys=True) if error else None
                event = ToolEvent(
                    event_id=event_id, call_id=call.id, tool_name=call.function, args=raw_args,
                    result=result, error=error_text, before_snapshot_id=before, after_snapshot_id=after,
                    message_index=message_index, executed=executed,
                )
                store.append("tool_event", store.serialize(event))
                event_ids.append(event_id)
                if error:
                    tool_errors.append({"event_id": event_id, **error})
                tool_message = {
                    "role": "tool", "tool_call": deepcopy(call), "tool_call_id": call.id,
                    "content": json.dumps({"executed": executed, "result": result, "error": error}, ensure_ascii=False, sort_keys=True),
                    "error": error_text,
                }
                messages.append(tool_message)
                store.append("tool_result", {"execution_id": execution_id, "event_id": event_id, "message": tool_message})
            if ended or blocked_reason or cancellation:
                break
            if not calls:
                termination = "no_tool_calls"
                break
    except Exception as exc:
        termination = "execution_error"
        execution_error = _error(exc)
        store.append("execution_error", {"execution_id": execution_id, "error": execution_error})
    final = store.save_snapshot(environment)
    store.serialize(messages)
    diagnostics = deepcopy([
        d for d in store.diagnostics if d["kind"] == "serialization_error"
    ][serialization_start:])
    record = ExecutionRecord(
        execution_id=execution_id, task_id=adapter.task_id, goal_condition=goal,
        messages=messages, tool_schema=schema, initial_snapshot_id=initial, final_snapshot_id=final,
        event_ids=event_ids, termination=termination,
        manifest={
            "schema_version": config.schema_version, "config": manifest_config,
            "actor_turns": turns, "complete_trace": not diagnostics and termination != "execution_error",
            "diagnostics": diagnostics, "tool_errors": tool_errors, "error": execution_error,
        }, fingerprint="",
    )
    store.save_execution(record)
    store.append("execution_frozen", {"execution_id": execution_id, "fingerprint": record.fingerprint, "termination": termination})
    if cancellation:
        raise cancellation
    return record
