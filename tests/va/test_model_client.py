"""Offline tests at the model/provider boundary, with SDK-shaped responses."""

import builtins
import asyncio
import copy
import json
import traceback
from types import SimpleNamespace

import pytest

from utils.functions_runtime import FunctionsRuntime
from va_experiment.models import ModelCapabilityError, ModelConfig
from va_experiment.model_client import LegacyModelClient


def config(provider="fake", model_id="fixture-model"):
    return ModelConfig(
        provider=provider, model_id=model_id, api_key_env="" if provider == "fake" else "VA_TEST_KEY",
        temperature=0.25, max_output_tokens=37,
    )


@pytest.fixture
def empty_runtime():
    return FunctionsRuntime([])


@pytest.fixture
def mutating_wrapped_client():
    class MutatingProvider:
        async def query(self, query, runtime, *, messages, extra_args):
            messages[0]["content"] = "changed by SDK conversion"
            extra_args.update(
                provider_raw_response={"content": "report", "reasoning": "provider reasoning"},
                provider_finish_reason="stop", provider_request_id="request-1", provider_usage={},
            )
            return SimpleNamespace(
                messages=[*messages, {"role": "assistant", "content": "report", "tool_calls": None}],
                extra_args=extra_args,
            )

    return LegacyModelClient(MutatingProvider(), config())


@pytest.mark.asyncio
async def test_message_mutation_is_not_shared(mutating_wrapped_client, empty_runtime):
    messages = [{"role": "user", "content": "original"}]
    before = copy.deepcopy(messages)
    reply = await mutating_wrapped_client.complete(messages, empty_runtime, purpose="report")
    assert messages == before
    assert reply.message["content"] == "report"
    assert reply.raw_response["provider_response"]["reasoning"] == "provider reasoning"


def test_fake_construction_needs_no_sdk_import_or_key(monkeypatch):
    from va_experiment.model_client import create_model_client

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"llms", "openai", "anthropic"}:
            raise AssertionError(f"Fake client imported {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert create_model_client(config()) is not None


@pytest.mark.asyncio
async def test_explicit_openai_settings_and_lossless_reply(monkeypatch, empty_runtime):
    from openai.types.chat import ChatCompletion
    import openai
    from va_experiment.model_client import create_model_client

    response = ChatCompletion.model_validate({
        "id": "completion-1", "object": "chat.completion", "created": 1, "model": "fixture-model",
        "choices": [{"index": 0, "finish_reason": "length", "message": {
            "role": "assistant", "content": "partial report", "reasoning_content": "raw reasoning",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "example", "arguments": '{ "x" : 1 }',
            }}],
        }}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
    })
    object.__setattr__(response, "_request_id", "request-openai-1")
    requests, credentials = [], []

    async def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        return response

    def sdk(**kwargs):
        credentials.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(openai, "AsyncOpenAI", sdk)
    monkeypatch.setenv("VA_TEST_KEY", "test-secret-must-not-be-recorded")
    client = create_model_client(config("openai"))
    reply = await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    assert credentials == [{"api_key": "test-secret-must-not-be-recorded", "max_retries": 0}]
    assert len(requests) == 1
    assert requests[0]["model"] == "fixture-model"
    assert requests[0]["temperature"] == 0.25
    assert requests[0]["max_tokens"] == 37
    assert "purpose" not in requests[0]
    assert reply.finish_reason == "length"
    assert reply.request_id == "request-openai-1"
    assert reply.usage["total_tokens"] == 13
    raw = reply.raw_response["provider_response"]
    assert raw["choices"][0]["message"]["reasoning_content"] == "raw reasoning"
    assert raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{ "x" : 1 }'
    assert [call["operation"] for call in reply.raw_response["calls"]] == ["chat.completions.create"]
    assert "test-secret" not in str(reply.raw_response)


@pytest.fixture
def anthropic_sdk(monkeypatch):
    import anthropic
    from anthropic._models import construct_type
    from anthropic.types import Message, MessageTokensCount
    from va_experiment.model_client import create_model_client

    calls = []
    state = {"input_tokens": 8, "stop_reason": "max_tokens", "completion_calls": 0}

    async def count_tokens(**kwargs):
        calls.append(("count_tokens", copy.deepcopy(kwargs)))
        if state.get("count_errors"):
            error = state["count_errors"].pop(0)
            if error is not None:
                raise error
        return MessageTokensCount(input_tokens=state["input_tokens"])

    async def create(**kwargs):
        calls.append(("create", copy.deepcopy(kwargs)))
        state["completion_calls"] += 1
        if "error" in state:
            raise state["error"]
        return construct_type(type_=Message, value={
            "id": "anthropic-response", "type": "message", "role": "assistant", "model": "fixture-model",
            "content": [{"type": "thinking", "thinking": "raw thought", "signature": "sig"},
                        {"type": "text", "text": "partial report"},
                        {"type": "tool_use", "id": "call-2", "name": "example", "input": {"x": 2}}],
            "stop_reason": state["stop_reason"], "stop_sequence": None,
            "usage": {"input_tokens": 8, "output_tokens": 3},
        })

    def sdk(**kwargs):
        assert kwargs == {"api_key": "anthropic-test-secret", "max_retries": 0}
        return SimpleNamespace(messages=SimpleNamespace(create=create, count_tokens=count_tokens))

    monkeypatch.setattr(anthropic, "AsyncAnthropic", sdk)
    monkeypatch.setenv("VA_TEST_KEY", "anthropic-test-secret")
    return create_model_client(config("anthropic")), calls, state


@pytest.mark.asyncio
async def test_anthropic_truncation_retains_reasoning_and_token_requests(anthropic_sdk, empty_runtime):
    client, calls, state = anthropic_sdk
    reply = await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    assert reply.finish_reason == "max_tokens"
    assert reply.message == {"role": "assistant", "content": "partial report", "tool_calls": None}
    assert reply.raw_response["provider_response"]["content"][0]["thinking"] == "raw thought"
    assert reply.raw_response["provider_response"]["content"][2]["input"] == {"x": 2}
    assert reply.usage["input_tokens"] == 8
    assert reply.usage["output_tokens"] == 3
    assert state["completion_calls"] == 1
    assert [item["operation"] for item in reply.raw_response["calls"]] == [
        "messages.count_tokens", "messages.create", "messages.count_tokens",
    ]
    assert reply.raw_response["calls"][0]["response"] == {"input_tokens": 8}
    completion_request = calls[1][1]
    assert {key: completion_request[key] for key in ("model", "temperature", "max_tokens")} == {
        "model": "fixture-model", "temperature": 0.25, "max_tokens": 37,
    }


@pytest.mark.asyncio
async def test_preflight_context_overflow_is_not_successful_end_task(anthropic_sdk, empty_runtime):
    client, _, state = anthropic_sdk
    state["input_tokens"] = 120000
    reply = await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    assert reply.finish_reason == "context_limit"
    assert reply.message["tool_calls"] is None
    assert reply.raw_response["provider_response"] is None
    assert reply.request_id == ""
    assert state["completion_calls"] == 0
    assert [item["operation"] for item in reply.raw_response["calls"]] == ["messages.count_tokens"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preflight", "output"])
async def test_anthropic_token_count_cancellation_propagates_with_evidence(anthropic_sdk, empty_runtime, stage):
    from va_experiment.model_client import ModelCallCancelledError

    client, requests, state = anthropic_sdk
    state["stop_reason"] = "end_turn"
    cancellation = asyncio.CancelledError("token count cancelled with anthropic-test-secret")
    state["count_errors"] = [cancellation] if stage == "preflight" else [None, cancellation]
    with pytest.raises(ModelCallCancelledError) as caught:
        await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")

    envelope = caught.value.raw_response
    assert envelope["calls"] == client.calls
    expected_operations = ["messages.count_tokens"]
    if stage == "output":
        expected_operations += ["messages.create", "messages.count_tokens"]
    assert [call["operation"] for call in envelope["calls"]] == expected_operations
    assert state["completion_calls"] == (0 if stage == "preflight" else 1)
    pending = envelope["calls"][-1]
    assert pending["request"] == requests[-1][1]
    assert pending["request"]["model"] == "fixture-model"
    assert pending["purpose"] == "report"
    assert pending["response"] is None
    assert pending["error"] == {
        "type": "CancelledError", "message": "token count cancelled with [REDACTED_API_KEY]", "body": None,
    }
    if stage == "preflight":
        assert envelope["provider_response"] is None
    else:
        assert envelope["provider_response"] == envelope["calls"][1]["response"]
        assert envelope["provider_response"]["id"] == "anthropic-response"
        assert envelope["provider_response"]["content"][0]["thinking"] == "raw thought"
    for output in (json.dumps(envelope, allow_nan=False), str(caught.value),
                   "".join(traceback.format_exception(caught.value))):
        assert "anthropic-test-secret" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preflight", "output"])
async def test_anthropic_token_count_ordinary_error_keeps_fallback(anthropic_sdk, empty_runtime, stage):
    client, _, state = anthropic_sdk
    state["stop_reason"] = "end_turn"
    failure = RuntimeError("token service unavailable")
    state["count_errors"] = [failure] if stage == "preflight" else [None, failure]
    reply = await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    assert reply.finish_reason == "end_turn"
    assert state["completion_calls"] == 1
    calls = reply.raw_response["calls"]
    assert [call["operation"] for call in calls] == ["messages.count_tokens", "messages.create", "messages.count_tokens"]
    failed_count = calls[0 if stage == "preflight" else 2]
    assert failed_count["response"] is None
    assert failed_count["error"]["type"] == "RuntimeError"
    assert failed_count["error"]["message"] == "token service unavailable"
    assert reply.usage["input_tokens"] > 0 and reply.usage["output_tokens"] > 0
    assert reply.raw_response["provider_response"]["id"] == "anthropic-response"


@pytest.fixture
def openai_sdk(monkeypatch):
    import openai
    from openai.types.chat import ChatCompletion
    from va_experiment.model_client import create_model_client

    response = ChatCompletion.model_validate({
        "id": "retry-response", "object": "chat.completion", "created": 1, "model": "fixture-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "report", "tool_calls": None,
        }}], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    })
    state = {"outcomes": [response], "requests": []}

    async def create(**kwargs):
        state["requests"].append(copy.deepcopy(kwargs))
        outcome = state["outcomes"].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    monkeypatch.setenv("VA_TEST_KEY", "test-key")
    return create_model_client(config("openai")), state, response


def bad_request(code, message="bad request", param=None):
    import httpx
    import openai

    return openai.BadRequestError(
        message, response=httpx.Response(400, request=httpx.Request("POST", "https://offline.invalid")),
        body={"code": code, "message": message, "param": param},
    )


@pytest.mark.asyncio
async def test_sdk_context_overflow_has_no_fabricated_completion(openai_sdk, empty_runtime):
    client, state, _ = openai_sdk
    state["outcomes"] = [bad_request("context_length_exceeded")]
    reply = await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    assert reply.finish_reason == "context_limit"
    assert reply.message == {"role": "assistant", "content": "", "tool_calls": None}
    assert reply.raw_response["provider_response"] is None
    assert reply.raw_response["calls"][0]["error"]["body"]["code"] == "context_length_exceeded"


@pytest.mark.asyncio
async def test_retry_attempts_are_individually_recorded(openai_sdk, monkeypatch, empty_runtime):
    from llms import openai_llm
    from tenacity import wait_none

    client, state, response = openai_sdk
    state["outcomes"] = [RuntimeError("transient SDK failure"), response]
    monkeypatch.setattr(openai_llm.chat_completion_request.retry, "wait", wait_none())
    reply = await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    assert len(state["requests"]) == 2
    calls = reply.raw_response["calls"]
    assert calls[0]["request"] == calls[1]["request"]
    assert [call["attempt_index"] for call in calls] == [1, 2]
    assert calls[0]["error"]["message"] == "transient SDK failure"
    assert calls[1]["response"]["id"] == "retry-response"
    assert reply.request_id == ""  # A completion ID is not a provider request ID.


@pytest.mark.asyncio
async def test_failure_exposes_serializable_journal(openai_sdk, empty_runtime):
    from va_experiment.model_client import ModelCallError

    client, state, _ = openai_sdk
    state["outcomes"] = [bad_request("invalid_request")]
    with pytest.raises(ModelCallError) as caught:
        await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    envelope = caught.value.raw_response
    assert envelope["calls"][0]["request"]["model"] == "fixture-model"
    assert envelope["calls"][0]["error"]["body"]["code"] == "invalid_request"
    json.dumps(envelope, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_sdk_cancellation_through_executor_retains_redacted_request(
    provider, request, tmp_path, mini_task, valid_config, capsys,
):
    from va_experiment.execution import execute_task
    from va_experiment.models import RunConfig
    from va_experiment.records import RecordStore

    fixture = request.getfixturevalue(f"{provider}_sdk")
    credential = "test-key" if provider == "openai" else "anthropic-test-secret"
    cancellation = asyncio.CancelledError(f"SDK cancelled with credential {credential}")
    if provider == "openai":
        client, state, _ = fixture
        state["outcomes"] = [cancellation]
    else:
        client, _, state = fixture
        state["error"] = cancellation
    store = RecordStore(tmp_path)
    valid_config["actor"] = client.config.model_dump()
    with pytest.raises(asyncio.CancelledError) as caught:
        await execute_task(mini_task(), "normal_goal", client, RunConfig(**valid_config), store)

    rows = RecordStore(tmp_path).read_journal()
    failed = next(row["payload"] for row in rows if row["kind"] == "model_error")
    envelope = failed["raw_response"]
    assert envelope == caught.value.raw_response
    assert envelope["provider_response"] is None
    completion_calls = [call for call in envelope["calls"] if call["operation"].endswith(".create")]
    assert len(completion_calls) == 1
    call = completion_calls[0]
    assert call["operation"] == ("chat.completions.create" if provider == "openai" else "messages.create")
    assert call["purpose"] == "actor_execution"
    if provider == "openai":
        assert len(state["requests"]) == 1
        assert call["request"] == state["requests"][0]
    else:
        assert state["completion_calls"] == 1
        assert call["request"] == next(kwargs for operation, kwargs in fixture[1] if operation == "create")
    assert call["request"]["model"] == "fixture-model"
    user_content = "Set x to 4." if provider == "openai" else [{"type": "text", "text": "Set x to 4."}]
    assert call["request"]["messages"][-1] == {"role": "user", "content": user_content}
    assert call["response"] is None
    assert call["error"]["type"] == "CancelledError"
    assert call["error"]["message"] == "SDK cancelled with credential [REDACTED_API_KEY]"
    assert not any(row["kind"] in {"model_response", "tool_started", "tool_event"} for row in rows)
    record = store.load_execution(rows[0]["payload"]["execution_id"])
    assert record.termination == "cancelled" and record.event_ids == []
    assert store.load_snapshot(record.final_snapshot_id) == {"values": {}}
    captured = capsys.readouterr()
    for output in (json.dumps(envelope), json.dumps(client.calls), str(caught.value),
                   "".join(traceback.format_exception(caught.value)), captured.out, captured.err):
        assert credential not in output


@pytest.mark.asyncio
async def test_unsupported_temperature_is_never_silently_replaced(openai_sdk, empty_runtime):
    client, state, _ = openai_sdk
    state["outcomes"] = [bad_request("unsupported_value", "temperature unsupported", "temperature")]
    with pytest.raises(ModelCapabilityError):
        await client.complete([{"role": "user", "content": "report"}], empty_runtime, purpose="report")
    assert len(state["requests"]) == 1
    assert state["requests"][0]["temperature"] == 0.25


@pytest.mark.parametrize("model_id", ["o1", "o3-mini", "o3", "o4-mini"])
def test_legacy_models_that_drop_temperature_are_rejected(model_id):
    from va_experiment.model_client import create_model_client

    with pytest.raises(ModelCapabilityError):
        create_model_client(config("openai", model_id))


@pytest.mark.asyncio
async def test_missing_metadata_is_diagnostic_error(empty_runtime):
    from va_experiment.model_client import ModelCallError

    class OpaqueProvider:
        async def query(self, *args, **kwargs):
            return SimpleNamespace(messages=[{"role": "assistant", "content": "looks successful"}], extra_args={})

    with pytest.raises(ModelCallError, match="metadata"):
        await LegacyModelClient(OpaqueProvider(), config()).complete([], empty_runtime, purpose="report")


@pytest.mark.asyncio
async def test_legacy_context_shape_is_retained(anthropic_sdk, empty_runtime):
    client, _, state = anthropic_sdk
    state["input_tokens"] = 120000
    result = await client.llm.query(
        "", empty_runtime, messages=[{"role": "user", "content": "report"}], extra_args={},
    )
    assert result.messages[-1]["tool_calls"][0].function == "end_task"
    assert result.extra_args["provider_finish_reason"] == "context_limit"
    assert result.extra_args["provider_raw_response"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_error_never_records_sdk_credential(provider, request, empty_runtime, capsys):
    from va_experiment.model_client import ModelCallError

    task_key = "sk-simulated-task-key-001"
    fixture = request.getfixturevalue(f"{provider}_sdk")
    credential = "test-key" if provider == "openai" else "anthropic-test-secret"
    message = f"credential {credential} rejected; Authorization: Bearer {credential}; task key {task_key}"
    if provider == "openai":
        client, state, _ = fixture
        state["outcomes"] = [bad_request("invalid_request", message)]
    else:
        import anthropic
        import httpx

        client, _, state = fixture
        state["error"] = anthropic.BadRequestError(
            message,
            response=httpx.Response(400, request=httpx.Request("POST", "https://offline.invalid")),
            body={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
        )
    with pytest.raises(ModelCallError) as caught:
        await client.complete(
            [{"role": "user", "content": f"Report task key {task_key}"}], empty_runtime, purpose="report",
        )
    captured = capsys.readouterr()
    for output in (captured.out, captured.err):
        assert credential not in output
        assert f"Bearer {credential}" not in output
    for journal in (caught.value.raw_response, client.calls):
        serialized = json.dumps(journal)
        assert credential not in serialized
        assert f"Bearer {credential}" not in serialized
        assert task_key in serialized
        assert "[REDACTED_API_KEY]" in serialized
    assert credential not in str(caught.value)
    rendered_error = "".join(traceback.format_exception(caught.value))
    assert credential not in rendered_error
    assert f"Bearer {credential}" not in rendered_error
    error_record = caught.value.raw_response["calls"][-1]["error"]
    assert error_record["type"] == "BadRequestError"
    assert "[REDACTED_API_KEY]" in error_record["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata, expected_raw", [
    (None, None),
    ({"provider_raw_response": {"original": "preserved"},
      "provider_finish_reason": None, "provider_request_id": None}, {"original": "preserved"}),
])
async def test_invalid_metadata_stays_diagnostic_and_keeps_evidence(empty_runtime, metadata, expected_raw):
    from va_experiment.model_client import ModelCallError

    class InvalidMetadataProvider:
        async def query(self, *args, **kwargs):
            return SimpleNamespace(messages=[], extra_args=metadata)

    with pytest.raises(ModelCallError) as caught:
        await LegacyModelClient(InvalidMetadataProvider(), config()).complete([], empty_runtime, purpose="report")
    assert caught.value.raw_response["provider_response"] == expected_raw
