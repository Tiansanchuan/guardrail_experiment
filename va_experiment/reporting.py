"""One-shot ordinary/VA reports from isolated copies of frozen actor history."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import random
from uuid import uuid4

from utils.functions_runtime import FunctionsRuntime
from va_experiment.models import ExecutionRecord, ModelClient, ModelReply, ReportRecord
from va_experiment.prompts import PROTOCOL_VERSION, report_request
from va_experiment.records import RecordStore, execution_fingerprint


def load_reports(store: RecordStore, execution_id: str) -> list[ReportRecord]:
    """Read all attempts in durable journal order, including failed attempts."""
    return [
        ReportRecord.model_validate(deepcopy(row["payload"])) for row in store.read_journal()
        if row["kind"] == "report_record" and row["payload"]["execution_id"] == execution_id
    ]


def select_first_valid_report(reports: list[ReportRecord], protocol: str) -> ReportRecord | None:
    """Select the first ok report from chronological attempts; never rank content."""
    report_request(protocol)  # Reject misspelled protocols rather than silently losing a branch.
    return next((report for report in reports if report.protocol == protocol and report.status == "ok"), None)


def _reply_status(reply: ModelReply) -> tuple[str, dict | None]:
    reason = reply.finish_reason
    status = {
        "context_limit": "context_limit", "length": "truncated", "max_tokens": "truncated",
        "refusal": "refused", "content_filter": "refused",
    }.get(reason)
    if status:
        return status, None
    if reason not in {"stop", "end_turn", "stop_sequence", "tool_calls", "tool_use"}:
        return "model_error", {"type": "UnexpectedFinishReason", "message": f"Report stopped with {reason!r}"}
    raw = reply.raw_response.get("provider_response")
    choices = raw.get("choices") if isinstance(raw, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and message.get("refusal"):
            return "refused", None
    if reply.message.get("tool_calls") or reason in {"tool_calls", "tool_use"}:
        return "tool_request", None
    return ("ok" if (reply.message.get("content") or "").strip() else "empty"), None


def _verify_frozen(execution: ExecutionRecord, frozen: ExecutionRecord, store: RecordStore) -> None:
    committed = store.load_execution(frozen.execution_id)
    if (
        execution_fingerprint(execution) != frozen.fingerprint
        or execution.fingerprint != frozen.fingerprint or committed.fingerprint != frozen.fingerprint
    ):
        raise ValueError("Report input differs from the committed execution")
    for snapshot_id in {frozen.initial_snapshot_id, frozen.final_snapshot_id}:
        store.load_snapshot(snapshot_id)


async def generate_reports(
    execution: ExecutionRecord, client: ModelClient, store: RecordStore, *, order_seed: int,
) -> list[ReportRecord]:
    """Generate one report per protocol without replaying or extending execution."""
    frozen = store.load_execution(execution.execution_id)
    _verify_frozen(execution, frozen, store)
    actor_config = frozen.manifest.get("config", {}).get("actor")
    client_config = getattr(client, "config", None)
    if actor_config is not None and client_config is not None and store.serialize(client_config) != actor_config:
        raise ValueError("Report client must use the original actor configuration")
    prefix = deepcopy(frozen.messages)
    prefix_fingerprint = hashlib.sha256(json.dumps(
        store.serialize(prefix), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    protocols = ["ordinary", "va_notice"]
    random.Random(order_seed).shuffle(protocols)
    previous = [
        row["payload"] for row in store.read_journal()
        if row["kind"] in {"report_request", "report_record"}
        and row["payload"]["execution_id"] == frozen.execution_id
    ]
    attempts = {
        protocol: 1 + max((r["attempt_index"] for r in previous if r["protocol"] == protocol), default=-1)
        for protocol in protocols
    }
    pair_id = uuid4().hex
    store.append("report_pair_started", {
        "pair_id": pair_id, "execution_id": frozen.execution_id,
        "execution_fingerprint": frozen.fingerprint, "prefix_fingerprint": prefix_fingerprint,
        "order_seed": order_seed, "protocol_order": protocols,
        "automatic_report_retries": 0,
        "inference_rng_support": "unknown", "source_termination": frozen.termination,
        "actor_config": actor_config, "client_actor_config": store.serialize(client_config),
        "actor_config_verification": "matched" if actor_config is not None and client_config is not None else "unavailable",
    })
    reports = []
    for protocol in protocols:
        request = report_request(protocol)
        report_id = uuid4().hex
        messages = deepcopy(prefix) + [{"role": "user", "content": request}]
        store.append("report_request", {
            "pair_id": pair_id, "report_id": report_id, "execution_id": frozen.execution_id,
            "protocol": protocol, "protocol_version": PROTOCOL_VERSION, "attempt_index": attempts[protocol],
            "prefix_fingerprint": prefix_fingerprint, "messages": messages,
            "tool_schema": [], "purpose": "actor_report",
        })
        reply = None
        cancellation = None
        try:
            reply = await client.complete(deepcopy(messages), FunctionsRuntime([]), purpose="actor_report")
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                cancellation = exc
            status, error = "model_error", {
                "type": type(exc).__name__, "message": str(exc),
                "raw_response": deepcopy(getattr(exc, "raw_response", None)),
            }
            store.append("report_error", {"pair_id": pair_id, "report_id": report_id, "error": error})
        else:
            store.append("report_response", {"pair_id": pair_id, "report_id": report_id, "reply": reply})
            status, error = _reply_status(reply)
        report = ReportRecord(
            report_id=report_id, execution_id=frozen.execution_id, protocol=protocol,
            protocol_version=PROTOCOL_VERSION, request=request, reply=reply.model_copy(deep=True) if reply else None,
            status=status, attempt_index=attempts[protocol], prefix_fingerprint=prefix_fingerprint, error=error,
        )
        store.append("report_record", store.serialize(report))
        reports.append(report)
        if cancellation is not None:
            store.append("report_pair_cancelled", {
                "pair_id": pair_id, "execution_id": frozen.execution_id,
                "report_ids": [report.report_id for report in reports],
            })
            raise cancellation
    try:
        _verify_frozen(execution, frozen, store)
    except Exception as exc:
        store.append("report_integrity_error", {
            "pair_id": pair_id, "execution_id": frozen.execution_id,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })
        raise
    store.append("report_pair_finished", {
        "pair_id": pair_id, "execution_id": frozen.execution_id,
        "report_ids": [report.report_id for report in reports],
        "execution_fingerprint": frozen.fingerprint,
    })
    return reports
