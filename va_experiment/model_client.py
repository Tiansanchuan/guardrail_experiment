"""Explicit, observable provider boundary; this module imports no provider SDK."""

from __future__ import annotations

import asyncio
import os
from copy import copy, deepcopy
from typing import TYPE_CHECKING, Any

from va_experiment.models import ModelCapabilityError, ModelClient, ModelConfig, ModelReply

if TYPE_CHECKING:
    from utils.functions_runtime import FunctionsRuntime
    from utils.types import ChatMessage


def _data(value: Any) -> Any:
    """Snapshot SDK models and request sentinels as plain JSON data."""
    if hasattr(value, "model_dump"):
        # Older SDK types may carry newer provider blocks as extra fields.
        return value.model_dump(mode="json", warnings=False)
    if isinstance(value, dict):
        return {str(key): _data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_data(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"sdk_type": type(value).__name__}


def _redact(value: Any, credential: str | None) -> Any:
    if not credential:
        return deepcopy(value)
    if isinstance(value, str):
        return value.replace(credential, "[REDACTED_API_KEY]")
    if isinstance(value, dict):
        return {_redact(key, credential): _redact(item, credential) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, credential) for item in value]
    return deepcopy(value)


class ModelCallError(RuntimeError):
    """A failed completion; ``raw_response`` has ``provider_response`` and ``calls``.

    Callers may persist this plain dictionary alongside the exception type/message.
    ``provider_response`` is null if no completion was received. Each call contains
    operation, purpose, attempt_index (one-based per operation within this logical
    completion), request, response, and error. SDK credential values are redacted.
    """

    def __init__(self, message: str, raw_response: dict[str, Any]):
        super().__init__(message)
        self.raw_response = deepcopy(raw_response)


class ModelCapabilityCallError(ModelCallError, ModelCapabilityError):
    """Provider rejection of a capability, retaining the failed-call journal."""


class ModelCallCancelledError(asyncio.CancelledError):
    """Cancellation remains cancellation, carrying the redacted request journal."""

    def __init__(self, message: str, raw_response: dict[str, Any]):
        super().__init__(message)
        self.raw_response = deepcopy(raw_response)


def _provider_error(exc: Exception) -> dict[str, Any]:
    body = _data(getattr(exc, "body", None))
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    return body if isinstance(body, dict) else {}


def _is_context_limit(exc: Exception) -> bool:
    error = _provider_error(exc)
    if error.get("code") in {"context_length_exceeded", "context_window_exceeded", "prompt_too_long"}:
        return True
    # Anthropic reports this as invalid_request_error without a specific code.
    return (
        error.get("type") == "invalid_request_error"
        and str(error.get("message", "")).lower().startswith("prompt is too long")
    )


class _JournalSDK:
    def __init__(self, target: Any, calls: list[dict], purpose: str, credential: str | None, path: str = ""):
        self.target, self.calls, self.purpose, self.path = target, calls, purpose, path
        self.credential = credential

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.target, name)
        path = f"{self.path}.{name}".lstrip(".")
        if name not in {"create", "count_tokens"}:
            return _JournalSDK(target, self.calls, self.purpose, self.credential, path)

        async def request(**kwargs):
            record = {
                "operation": path, "purpose": self.purpose,
                "attempt_index": sum(item["operation"] == path for item in self.calls) + 1,
                "request": _redact(_data(kwargs), self.credential), "response": None, "error": None,
            }
            self.calls.append(record)
            try:
                response = await target(**kwargs)
            except (Exception, asyncio.CancelledError) as exc:
                record["error"] = _redact({
                    "type": type(exc).__name__, "message": str(exc),
                    "body": _data(getattr(exc, "body", None)),
                }, self.credential)
                raise
            record["response"] = _redact(_data(response), self.credential)
            return response

        return request


class _UnscriptedFake:
    async def query(self, *args, **kwargs):
        raise ModelCapabilityError("Fake completions require an injected scripted client")


def create_model_client(config: ModelConfig) -> ModelClient:
    """Construct only the explicit provider/model, using the named credential."""
    if config.provider == "fake":
        return LegacyModelClient(_UnscriptedFake(), config)
    if config.provider == "openai" and config.model_id in {"o1", "o3-mini", "o3", "o4-mini"}:
        raise ModelCapabilityError("Legacy OpenAI adapter cannot honor explicit temperature for this model")
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise ValueError(f"Credential environment variable {config.api_key_env} is empty or missing")
    # Preserve the repository's provider registration, while bypassing YAML routing
    # and each provider's historical create_client credential selection.
    from llms.base import LLMFactory

    if config.provider == "openai":
        from openai import AsyncOpenAI
        sdk = AsyncOpenAI(api_key=api_key, max_retries=0)
    elif config.provider == "anthropic":
        from anthropic import AsyncAnthropic
        sdk = AsyncAnthropic(api_key=api_key, max_retries=0)
    else:
        raise ModelCapabilityError(f"Unsupported provider: {config.provider}")
    llm = LLMFactory.create(
        config.provider, config.model_id, client=sdk,
        temperature=config.temperature, max_tokens=config.max_output_tokens,
    )
    return LegacyModelClient(llm, config)


class LegacyModelClient:
    def __init__(self, llm: Any, config: ModelConfig):
        self.llm = llm
        self.config = config.model_copy(deep=True)
        self.calls: list[dict[str, Any]] = []
        self._credential = getattr(getattr(llm, "client", None), "api_key", None) or os.environ.get(config.api_key_env)

    async def complete(
        self, messages: list[ChatMessage], runtime: FunctionsRuntime, *, purpose: str,
    ) -> ModelReply:
        calls: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        llm = copy(self.llm)
        if hasattr(llm, "client"):
            llm.client = _JournalSDK(llm.client, calls, purpose, self._credential)
        # Historical Anthropic pricing is incomplete; unknown cost is not zero.
        if hasattr(llm, "_MODEL_PRICING") and llm.model not in llm._MODEL_PRICING:
            llm.calculate_cost = lambda *_: None
        try:
            result = await llm.query("", runtime, messages=deepcopy(messages), extra_args=metadata)
            if not isinstance(result.extra_args, dict):
                raise RuntimeError("Provider supplied invalid observable metadata envelope")
            metadata = result.extra_args
            required = ("provider_raw_response", "provider_finish_reason", "provider_request_id")
            if any(key not in metadata for key in required):
                raise RuntimeError("Provider omitted observable completion metadata")
            if not isinstance(metadata["provider_finish_reason"], str) or not metadata["provider_finish_reason"]:
                raise RuntimeError("Provider supplied invalid finish metadata")
            if metadata["provider_finish_reason"] == "context_limit":
                # The legacy adapter synthesizes end_task on preflight overflow.
                message = {"role": "assistant", "content": "", "tool_calls": None}
            else:
                if not isinstance(metadata["provider_raw_response"], dict):
                    raise RuntimeError("Provider omitted its raw completion response")
                message = deepcopy(result.messages[-1])
                if message["role"] != "assistant":
                    raise RuntimeError("Provider did not return an assistant message")
            return ModelReply(
                message=message,
                raw_response=_redact(
                    {"provider_response": metadata["provider_raw_response"], "calls": calls}, self._credential,
                ),
                finish_reason=metadata["provider_finish_reason"],
                usage=deepcopy(metadata.get("provider_usage", {})),
                request_id=metadata["provider_request_id"],
            )
        except asyncio.CancelledError as exc:
            envelope = _redact(
                {"provider_response": metadata.get("provider_raw_response"), "calls": calls}, self._credential,
            )
            raise ModelCallCancelledError(_redact(str(exc), self._credential), envelope) from None
        except Exception as exc:
            envelope = _redact(
                {"provider_response": metadata.get("provider_raw_response"), "calls": calls}, self._credential,
            )
            if _is_context_limit(exc):
                return ModelReply(
                    message={"role": "assistant", "content": "", "tool_calls": None},
                    raw_response=envelope, finish_reason="context_limit", usage={},
                    request_id=getattr(exc, "request_id", None) or "",
                )
            error = _provider_error(exc)
            if isinstance(exc, ModelCapabilityError) or error.get("code") in {
                "unsupported_value", "unsupported_parameter", "model_not_found",
            }:
                # The original SDK error is preserved, redacted, in the journal.
                # Chaining it would render its unredacted message in tracebacks.
                raise ModelCapabilityCallError(_redact(str(exc), self._credential), envelope) from None
            raise ModelCallError(_redact(str(exc), self._credential), envelope) from None
        finally:
            self.calls.extend(deepcopy(calls))
