"""Shared read-only task evidence helpers. Source data remains unchanged."""

from copy import deepcopy
from importlib import import_module
from pathlib import Path
import hashlib
import json

from pydantic import BaseModel, TypeAdapter

from va_experiment.models import TaskResult, Verdict

ROOT = Path(__file__).resolve().parents[2]
RUBRIC_VERSION = "audited-task-rules-v1"


def verdict(passed, score, refs, **components):
    return Verdict(status="unknown" if passed is None else "pass" if passed else "fail",
                   score=score, method="rule", rubric_version=RUBRIC_VERSION,
                   evidence_refs=refs, components=components)


def key_events(record, store):
    """All calls, including normal, unsuccessful and unexecuted requests."""
    return [dict(key_event_id=f"action-{e.event_id}", event_id=e.event_id,
                 tool_name=e.tool_name, args=deepcopy(e.args), result=deepcopy(e.result),
                 error=e.error, executed=e.executed,
                 evidence_refs=[e.event_id, e.before_snapshot_id, e.after_snapshot_id])
            for e in (store.load_event(i) for i in record.event_ids)]


def restore_fields(model, data):
    """Repair legacy validators which reset live collections to initial fixtures.

    First validate using the actual environment class. Then validate and restore
    declared fields recursively, because Inbox and SpamEmail constructors replace
    supplied live state. Computed fields and private tools are never assigned.
    """
    if isinstance(model, BaseModel):
        for name, field in type(model).model_fields.items():
            if name in data:
                value = TypeAdapter(field.annotation).validate_python(deepcopy(data[name]))
                setattr(model, name, restore_fields(value, data[name]))
        if type(model).__name__ == "Inbox":
            # Canonical JSON sorts mapping keys, but derived sent/received lists
            # retain insertion order. Rebuild that order from the frozen lists.
            ordered_ids = [e["id_"] for kind in ("received", "sent", "drafts")
                           for e in data.get(kind, [])]
            ordered_ids += list(model.emails)
            model.emails = {key: model.emails[key] for key in dict.fromkeys(ordered_ids)
                            if key in model.emails}
    elif isinstance(model, dict):
        for key in model:
            model[key] = restore_fields(model[key], data[str(key)])
    elif isinstance(model, list):
        for index in range(len(model)):
            model[index] = restore_fields(model[index], data[index])
    elif isinstance(data, (int, float)) and not isinstance(data, bool) and model == data:
        # Some legacy constructors assign integer zero to float fields. Preserve
        # canonical snapshot numeric representation after validating its value.
        return data
    return model


class RuleAdapter:
    task_id: str
    environment_module: str
    environment_name: str
    clean_side_goal: str
    audit_notes: list[str] = []
    reference_facts: dict = {}

    @property
    def normal_task(self):
        # Only access the original user query; never construct legacy system/HINT.
        definition = import_module(f"task_pairs.{self.task_id}.task_definition").TaskDefinition
        return definition.user_query.fget(None)

    def environment_class(self):
        return getattr(import_module(f"environments.{self.environment_module}"), self.environment_name)

    def create_environment(self):
        return self.environment_class().create()

    def restore_environment(self, snapshot):
        data = deepcopy(snapshot)
        return restore_fields(self.environment_class().model_validate(data), data)

    def reference_manifest(self):
        """Stable source hashes and facts for execution manifests / blind bundles."""
        files = sorted(p for p in (ROOT / "universe_related_stuff").rglob("*")
                       if p.is_file() and p.suffix in {".yaml", ".json"})
        files += [ROOT / f"task_pairs/{self.task_id}/task_definition.py",
                  ROOT / f"task_evaluation/{self.task_id}_evaluation.py"]
        sources = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
        return {"rubric_version": RUBRIC_VERSION, "task_id": self.task_id,
                "data_fingerprint": fingerprint, "source_sha256": sources,
                "reference_facts": deepcopy(self.reference_facts), "audit_notes": list(self.audit_notes)}

    def score_execution(self, record, store):
        events = []
        try:
            if record.task_id != self.task_id:
                raise ValueError("Execution task ID does not match adapter")
            events = key_events(record, store)
            initial = store.load_snapshot(record.initial_snapshot_id)
            final = store.load_snapshot(record.final_snapshot_id)
            return self.score(record, initial, final, events)
        except (ValueError, KeyError, TypeError, AttributeError, OSError, ZeroDivisionError) as exc:
            refs = [record.initial_snapshot_id, record.final_snapshot_id]
            unknown = verdict(None, None, refs, diagnostic=f"{type(exc).__name__}: {exc}")
            return TaskResult(execution_id=record.execution_id, main=unknown,
                              side_outcome_observed=unknown.model_copy(deep=True),
                              violation_status="unknown", findings=[{
                                  "kind": "rule_error", "detail": str(exc), "evidence_refs": refs,
                                  "source_refs": [],
                              }], legacy={}, semantic_job_ids=[], key_events=events)

    def semantic_jobs(self, record, result, store):
        return []
