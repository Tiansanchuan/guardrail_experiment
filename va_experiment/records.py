"""Durable, single-writer evidence journal and immutable execution artifacts."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from va_experiment.models import ExecutionRecord, ToolEvent


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def pure_data(value: Any, diagnostics: list[dict] | None = None) -> Any:
    """Canonical JSON data; unsupported/cyclic values produce explicit diagnostics."""
    diagnostics = diagnostics if diagnostics is not None else []
    active: set[int] = set()

    def failure(item, reason):
        detail = {"type": type(item).__name__, "reason": reason}
        diagnostics.append({"kind": "serialization_error", **detail})
        return {"__serialization_error__": detail}

    def convert(item):
        if isinstance(item, Enum):
            return convert(item.value)
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else failure(item, "non-finite number")
        if isinstance(item, (datetime, date, time)):
            return item.isoformat()
        if isinstance(item, Path):
            return str(item)
        if id(item) in active:
            return failure(item, "cyclic value")
        active.add(id(item))
        try:
            if isinstance(item, BaseModel):
                try:
                    return convert(item.model_dump(mode="python", warnings=False))
                except Exception as exc:
                    return failure(item, f"model_dump failed: {type(exc).__name__}: {exc}")
            if isinstance(item, dict):
                result = {}
                for key, child in item.items():
                    if not isinstance(key, (str, int, float, bool, Enum)) and key is not None:
                        failure(key, "unsupported mapping key")
                    text_key = str(key.value if isinstance(key, Enum) else key)
                    if text_key in result:
                        failure(key, "mapping key collision after JSON conversion")
                    result[text_key] = convert(child)
                return result
            if isinstance(item, (list, tuple)):
                return [convert(child) for child in item]
            if isinstance(item, (set, frozenset)):
                return sorted((convert(child) for child in item), key=_bytes)
            return failure(item, "unsupported value")
        finally:
            active.remove(id(item))

    return convert(value)


def execution_fingerprint(record: ExecutionRecord) -> str:
    """SHA-256 of committed canonical execution data, excluding the hash field itself."""
    data = pure_data(record)
    data.pop("fingerprint")
    return hashlib.sha256(_bytes(data)).hexdigest()


class RecordStore:
    """Append and fsync evidence; existing snapshots/executions cannot be overwritten.

    One writer owns a root at a time. Readers return fresh pure data/typed records.
    A torn journal tail is readable but blocks further writes until investigated.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ("snapshots", "executions"):
            (self.root / directory).mkdir(exist_ok=True)
        self.journal_path = self.root / "journal.jsonl"
        self.diagnostics: list[dict] = []
        self._lock = RLock()
        self._damaged_tail = False
        self.read_journal()

    def _diagnose(self, detail: dict) -> None:
        if detail not in self.diagnostics:
            self.diagnostics.append(detail)

    def serialize(self, value: Any) -> Any:
        return pure_data(value, self.diagnostics)

    def read_journal(self) -> list[dict]:
        rows = []
        if not self.journal_path.exists():
            return rows
        raw = self.journal_path.read_bytes()
        for index, line in enumerate(raw.splitlines(keepends=True), 1):
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("unterminated journal entry")
                entry = json.loads(line)
                if not isinstance(entry, dict) or not {"id", "kind", "payload"} <= entry.keys():
                    raise ValueError("invalid journal envelope")
            except (ValueError, UnicodeDecodeError) as exc:
                self._damaged_tail = True
                self._diagnose({"kind": "incomplete_journal", "line": index, "error": str(exc)})
                break
            rows.append(entry)
        self.diagnostics[:] = [d for d in self.diagnostics if d["kind"] not in {"incomplete_tool", "incomplete_execution"}]
        completed = {row["payload"]["event_id"] for row in rows if row["kind"] == "tool_event"}
        frozen = {row["payload"]["execution_id"] for row in rows if row["kind"] == "execution_frozen"}
        for row in rows:
            if row["kind"] == "tool_started" and row["payload"]["event_id"] not in completed:
                self._diagnose({"kind": "incomplete_tool", "event_id": row["payload"]["event_id"]})
            if row["kind"] == "execution_started" and row["payload"]["execution_id"] not in frozen:
                self._diagnose({"kind": "incomplete_execution", "execution_id": row["payload"]["execution_id"]})
        return rows

    def append(self, kind: str, payload: dict) -> str:
        with self._lock:
            if self._damaged_tail:
                raise ValueError("Cannot append after an incomplete journal tail")
            entry_id = uuid4().hex
            encoded = _bytes({"id": entry_id, "kind": kind, "payload": self.serialize(payload)}) + b"\n"
            with self.journal_path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            return entry_id

    @staticmethod
    def _safe_id(identifier: str) -> str:
        if not identifier or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in identifier):
            raise ValueError("Invalid artifact ID")
        return identifier

    def _immutable_write(self, path: Path, encoded: bytes) -> None:
        with self._lock:
            if path.exists():
                if path.read_bytes() != encoded:
                    raise ValueError(f"Immutable artifact already exists: {path.name}")
                return
            with path.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())

    def save_snapshot(self, environment) -> str:
        encoded = _bytes(self.serialize(environment))
        snapshot_id = hashlib.sha256(encoded).hexdigest()
        self._immutable_write(self.root / "snapshots" / f"{snapshot_id}.json", encoded)
        return snapshot_id

    def load_snapshot(self, snapshot_id: str) -> dict:
        path = self.root / "snapshots" / f"{self._safe_id(snapshot_id)}.json"
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != snapshot_id:
            raise ValueError("Snapshot fingerprint mismatch")
        return json.loads(encoded)

    def load_event(self, event_id: str) -> ToolEvent:
        for entry in self.read_journal():
            if entry["kind"] == "tool_event" and entry["payload"]["event_id"] == event_id:
                return ToolEvent.model_validate(deepcopy(entry["payload"]))
        raise KeyError(event_id)

    def save_execution(self, record: ExecutionRecord) -> None:
        record.fingerprint = execution_fingerprint(record)
        path = self.root / "executions" / f"{self._safe_id(record.execution_id)}.json"
        self._immutable_write(path, _bytes(self.serialize(record)))

    def load_execution(self, execution_id: str) -> ExecutionRecord:
        path = self.root / "executions" / f"{self._safe_id(execution_id)}.json"
        record = ExecutionRecord.model_validate_json(path.read_bytes())
        if execution_fingerprint(record) != record.fingerprint:
            raise ValueError("Execution fingerprint mismatch")
        return record
