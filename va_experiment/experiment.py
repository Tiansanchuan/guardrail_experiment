"""Explicit CLI and single-run orchestration; importing creates no SDK clients."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
from uuid import uuid4

import yaml

from va_experiment.config import load_config
from va_experiment.models import EvidenceBundle, ModelConfig, ModelClient, RunConfig, TaskResult
from va_experiment.records import RecordStore, pure_data
from va_experiment.task_adapters import get_task_adapter


def validate_config(config: RunConfig) -> dict:
    """Resolve every task before credentials, client construction or output writes."""
    from va_experiment.semantic_assessment import SemanticAdapter
    if len(set(config.task_ids)) != len(config.task_ids):
        raise ValueError("Duplicate task IDs are not allowed")
    adapters = {task_id: get_task_adapter(task_id) for task_id in config.task_ids}
    if config.assessment is None:
        semantic = [task_id for task_id, adapter in adapters.items() if isinstance(adapter, SemanticAdapter)]
        if semantic:
            raise ValueError(f"Semantic task scoring requires an assessment model: {semantic}")
    return adapters


def _write_new(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(pure_data(value), stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(pure_data(value), sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _provenance() -> dict:
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        result = subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", *args],
            cwd=root, capture_output=True, timeout=20)
        if result.returncode:
            raise ValueError("Cannot capture Git provenance")
        return result.stdout
    # Record source hashes too: git diff excludes untracked code under development.
    source_files = list((root / "va_experiment").rglob("*.py"))
    source_files += list((root / "va_experiment" / "rubrics").glob("*.json"))
    source_files += [root / "run_va_experiment.py", root / "requirements-va.txt"]
    return {"created_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version, "python_executable": sys.executable, "platform": platform.platform(),
        "resolved_dependencies": sorted({f"{dist.metadata['Name']}=={dist.version}" for dist in metadata.distributions()}),
        "git_commit": git("rev-parse", "HEAD").decode().strip(),
        "git_dirty_diff_sha256": hashlib.sha256(git("diff", "HEAD", "--binary")).hexdigest(),
        "git_status": git("status", "--porcelain").decode("utf-8", errors="replace"),
        "source_sha256": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(source_files) if p.is_file()}}


def _check_model(config: ModelConfig, *, injected: bool) -> None:
    if injected:
        return
    if config.provider == "fake":
        raise ValueError("Fake models require explicitly injected scripted clients; CLI run needs real model settings")
    if not os.environ.get(config.api_key_env):
        raise ValueError(f"Credential environment variable {config.api_key_env} is empty or missing")


def _fresh_directory(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"{prefix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex}"
    path.mkdir(exist_ok=False)
    return path


async def run_experiment(config: RunConfig, clients: dict[str, ModelClient] | None = None) -> Path:
    """Execute each assigned goal once, fork reports, then assess shuffled frozen data."""
    from va_experiment import model_client
    from va_experiment.assessment import (REPORT_ASSESSMENT_VERSION, assess_report,
                                         build_evidence_bundle)
    from va_experiment.execution import execute_task
    from va_experiment.prompts import PROTOCOL_VERSION
    from va_experiment.reporting import generate_reports
    from va_experiment.semantic_assessment import VERSION, apply_task_verdicts, assess_task_jobs

    # Pydantic validation also protects direct callers passing model_copy updates.
    config = RunConfig.model_validate(config.model_dump())
    config = config.model_copy(update={"output_dir": config.output_dir.resolve()})
    adapters = validate_config(config)
    clients = dict(clients or {})
    if set(clients) - {"actor", "assessment"}:
        raise ValueError("Injected client roles must be actor and assessment")
    roles = ("actor", "assessment") if config.assessment is not None else ("actor",)
    if set(clients) - set(roles):
        raise ValueError("Assessment client injected although assessment is disabled")
    for role in roles:
        _check_model(getattr(config, role), injected=role in clients)
        if role in clients and not callable(getattr(clients[role], "complete", None)):
            raise ValueError(f"Invalid injected {role} client")
    references = {task_id: adapter.reference_manifest() for task_id, adapter in adapters.items()}
    provenance = _provenance()
    for role in roles:
        if role not in clients:
            clients[role] = model_client.create_model_client(getattr(config, role))
    output = _fresh_directory(config.output_dir, "run")
    manifest = {"kind": "va_run", "schema_version": config.schema_version, "config": config,
        "assessment_rubric_version": REPORT_ASSESSMENT_VERSION, "task_semantic_rubric_version": VERSION,
        "report_protocol_version": PROTOCOL_VERSION,
        "planned_executions": 2 * len(adapters) * config.repetitions_per_goal,
        "automatic_execution_retries": 0, "assessment_enabled": config.assessment is not None,
        **provenance}
    _write_new(output / "manifest.json", manifest)
    for task_id, reference in references.items():
        _write_new(output / "task_manifests" / f"{task_id}.json", reference)
    store = RecordStore(output)
    jobs = []
    try:
        for task_id, adapter in adapters.items():
            for repetition in range(config.repetitions_per_goal):
                for goal in ("normal_goal", "side_goal"):
                    execution = await execute_task(adapter, goal, clients["actor"], config, store)
                    store.append("execution_task_manifest", {"execution_id": execution.execution_id,
                        "task_id": task_id, "repetition": repetition,
                        "path": f"task_manifests/{task_id}.json",
                        "data_fingerprint": references[task_id]["data_fingerprint"]})
                    if adapter.reference_manifest() != references[task_id]:
                        raise ValueError(f"Task sources changed since pre-execution manifest: {task_id}")
                    result = adapter.score_execution(execution, store)
                    semantic_jobs = adapter.semantic_jobs(execution, result, store)
                    if semantic_jobs:
                        verdicts = await assess_task_jobs(semantic_jobs, clients["assessment"], store)
                        result = apply_task_verdicts(result, semantic_jobs, verdicts)
                    store.append("task_result", result.model_dump())
                    _write_new(output / "results" / f"{execution.execution_id}.json", result)
                    reports = await generate_reports(execution, clients["actor"], store,
                        order_seed=config.order_seed + len(jobs))
                    if adapter.reference_manifest() != references[task_id]:
                        raise ValueError(f"Task sources changed since pre-execution manifest: {task_id}")
                    for report in reports:
                        blind_id = uuid4().hex
                        evidence = build_evidence_bundle(execution, result, store, blind_id=blind_id)
                        _write_new(output / "evidence" / f"{report.report_id}.json", evidence)
                        _write_new(output / "evidence_links" / f"{report.report_id}.json", {
                            "report_id": report.report_id, "execution_id": execution.execution_id,
                            "execution_fingerprint": execution.fingerprint,
                            "prefix_fingerprint": report.prefix_fingerprint,
                            "report_fingerprint": _fingerprint(report),
                            "task_result_fingerprint": _fingerprint(result),
                            "evidence_fingerprint": _fingerprint(evidence)})
                        store.append("private_report_mapping", {"report_id": report.report_id,
                            "execution_id": execution.execution_id, "blind_id": blind_id})
                        jobs.append((report, evidence))
        if config.assessment is not None:
            random.Random(config.order_seed).shuffle(jobs)
            store.append("assessment_schedule", {"report_ids": [r.report_id for r, _ in jobs],
                                                "order_seed": config.order_seed})
            for report, evidence in jobs:
                await assess_report(report, evidence, clients["assessment"], store)
    except BaseException as exc:
        store.append("run_interrupted", {"type": type(exc).__name__})
        summarize_run(output)
        raise
    store.append("run_finished", {"executions": 2 * len(adapters) * config.repetitions_per_goal})
    summarize_run(output)
    return output


def _source_records(source: Path):
    from va_experiment.reporting import load_reports
    store = RecordStore(source)
    order = [row["payload"]["execution_id"] for row in store.read_journal() if row["kind"] == "execution_frozen"]
    executions = [store.load_execution(identifier) for identifier in dict.fromkeys(order)]
    results = [TaskResult.model_validate(_read(source / "results" / f"{e.execution_id}.json"))
               for e in executions if (source / "results" / f"{e.execution_id}.json").exists()]
    reports = [report for e in executions for report in load_reports(store, e.execution_id)]
    return store, executions, results, reports


def summarize_run(run_dir: Path) -> Path:
    """Summarize one assessment session in durable chronological attempt order."""
    from va_experiment.analysis import summarize_experiment, write_summary
    from va_experiment.assessment import load_assessments
    run_dir = Path(run_dir).resolve()
    manifest = _read(run_dir / "manifest.json")
    source = Path(manifest.get("source_run_dir", run_dir))
    _, executions, results, reports = _source_records(source)
    assessments = load_assessments(RecordStore(run_dir))
    summary = summarize_experiment(executions, results, reports, assessments)
    summary["assessment_session"] = str(run_dir)
    summary["requested_rubric_version"] = manifest["assessment_rubric_version"]
    write_summary(summary, run_dir)
    return run_dir / "summary.json"


def load_assessment_config(path: Path) -> dict:
    """A re-evaluation config contains assessment settings and installed rubric ID."""
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"assessment", "rubric_version"}:
        raise ValueError("Re-evaluation config requires exactly assessment and rubric_version")
    if not isinstance(value["rubric_version"], str) or not value["rubric_version"].strip():
        raise ValueError("rubric_version must be explicit")
    return {"assessment_config": ModelConfig.model_validate(value["assessment"]),
            "rubric_version": value["rubric_version"]}


async def assess_existing(run_dir: Path, assessment_config: ModelConfig, *, rubric_version: str) -> Path:
    """Assess saved report/evidence pairs into a fresh journal, without actor execution."""
    from va_experiment import model_client
    from va_experiment.assessment import REPORT_ASSESSMENT_VERSION, assess_report
    if rubric_version != REPORT_ASSESSMENT_VERSION:
        raise ValueError(f"Requested rubric {rubric_version!r} differs from installed {REPORT_ASSESSMENT_VERSION!r}")
    assessment_config = ModelConfig.model_validate(assessment_config.model_dump())
    _check_model(assessment_config, injected=False)
    run_dir = Path(run_dir).resolve()
    original_manifest = _read(run_dir / "manifest.json")
    source = Path(original_manifest.get("source_run_dir", run_dir))
    # Load every immutable bundle before model construction; no live task sources.
    jobs = _frozen_jobs(source)
    provenance = _provenance()
    client = model_client.create_model_client(assessment_config)
    output = _fresh_directory(source.parent, f"assessment-{source.name}")
    _write_new(output / "manifest.json", {"kind": "va_reassessment", "source_run_dir": str(source),
        "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
        "assessment_config": assessment_config, "assessment_rubric_version": rubric_version, **provenance})
    store = RecordStore(output)
    order_seed = original_manifest.get("config", {}).get("order_seed", 0)
    random.Random(order_seed).shuffle(jobs)
    store.append("assessment_schedule", {"report_ids": [r.report_id for r, _ in jobs], "order_seed": order_seed})
    try:
        for report, evidence in jobs:
            _write_new(output / "evidence" / f"{report.report_id}.json", evidence)
            store.append("private_report_mapping", {"report_id": report.report_id,
                "execution_id": report.execution_id, "blind_id": evidence.blind_id})
            await assess_report(report, evidence, client, store)
    except BaseException as exc:
        store.append("assessment_interrupted", {"type": type(exc).__name__})
        summarize_run(output)
        raise
    summarize_run(output)
    return output


def _frozen_jobs(source: Path) -> list:
    """Saved report/evidence pairs, integrity-checked, each with a fresh blind ID."""
    _, executions, results, reports = _source_records(source)
    execution_by_id = {record.execution_id: record for record in executions}
    result_by_id = {record.execution_id: record for record in results}
    jobs = []
    for report in reports:
        evidence = EvidenceBundle.model_validate(_read(source / "evidence" / f"{report.report_id}.json"))
        association = _read(source / "evidence_links" / f"{report.report_id}.json")
        expected = {"report_id": report.report_id, "execution_id": report.execution_id,
            "execution_fingerprint": execution_by_id[report.execution_id].fingerprint,
            "prefix_fingerprint": report.prefix_fingerprint, "report_fingerprint": _fingerprint(report),
            "task_result_fingerprint": _fingerprint(result_by_id[report.execution_id]),
            "evidence_fingerprint": _fingerprint(evidence)}
        if association != expected:
            raise ValueError(f"Frozen report/evidence association fingerprint mismatch: {report.report_id}")
        jobs.append((report, evidence.model_copy(update={"blind_id": uuid4().hex})))
    return jobs


class _ExternalAnswer:
    """Feeds one saved external assessor answer through the normal assessment path."""

    def __init__(self, text: str, assessor: str, path: Path):
        self.config = {"provider": "external", "model_id": assessor, "api_key_env": None,
                       "temperature": None, "max_output_tokens": None}
        self.text, self.path = text, path

    async def complete(self, messages, runtime, *, purpose):
        from va_experiment.models import ModelReply
        return ModelReply(message={"role": "assistant", "content": self.text, "tool_calls": None},
            raw_response={"external_response_file": self.path.name,
                          "response_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest()},
            finish_reason="stop", usage={}, request_id="")


def export_assessments(run_dir: Path) -> Path:
    """Write blind requests for an external assessor; no model is called.

    Each ``requests/<blind_id>/`` holds the exact system text and data envelope an API
    assessor receives. Answers go to ``responses/<blind_id>.json``.
    """
    from va_experiment.assessment import REPORT_ASSESSMENT_VERSION, _hash, assessment_messages, report_text
    run_dir = Path(run_dir).resolve()
    source = Path(_read(run_dir / "manifest.json").get("source_run_dir", run_dir))
    jobs = _frozen_jobs(source)
    output = _fresh_directory(source.parent, f"external-assessment-{source.name}")
    _write_new(output / "manifest.json", {"kind": "va_external_assessment", "source_run_dir": str(source),
        "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
        "assessment_rubric_version": REPORT_ASSESSMENT_VERSION, **_provenance()})
    store = RecordStore(output)
    order_seed = _read(source / "manifest.json").get("config", {}).get("order_seed", 0)
    random.Random(order_seed).shuffle(jobs)
    store.append("assessment_schedule", {"report_ids": [r.report_id for r, _ in jobs], "order_seed": order_seed})
    for report, evidence in jobs:
        _write_new(output / "evidence" / f"{report.report_id}.json", evidence)
        store.append("private_report_mapping", {"report_id": report.report_id,
            "execution_id": report.execution_id, "blind_id": evidence.blind_id})
        messages = assessment_messages(report_text(report), evidence)
        folder = output / "requests" / evidence.blind_id
        folder.mkdir(parents=True)
        (folder / "instructions.md").write_text(messages[0]["content"], encoding="utf-8")
        # Indented so a file-reading assessor can page through it.
        _write_new(folder / "input.json", json.loads(messages[1]["content"]))
        store.append("external_assessment_request", {"blind_id": evidence.blind_id,
            "report_id": report.report_id, "request_sha256": _hash(messages)})
    return output


async def import_assessments(session_dir: Path, *, assessor: str) -> Path:
    """Validate saved external answers exactly like API replies; missing answers wait."""
    from va_experiment.assessment import _hash, assess_report, assessment_messages, report_text
    if not assessor.strip():
        raise ValueError("Name the external assessor for provenance")
    session_dir = Path(session_dir).resolve()
    manifest = _read(session_dir / "manifest.json")
    if manifest.get("kind") != "va_external_assessment":
        raise ValueError("Not an external assessment session")
    _, _, _, reports = _source_records(Path(manifest["source_run_dir"]))
    report_by_id = {report.report_id: report for report in reports}
    store = RecordStore(session_dir)
    rows = store.read_journal()
    done = {row["payload"]["report_id"] for row in rows if row["kind"] == "report_assessment_record"}
    waiting = []
    for request in [row["payload"] for row in rows if row["kind"] == "external_assessment_request"]:
        if request["report_id"] in done:
            continue
        answer = session_dir / "responses" / f"{request['blind_id']}.json"
        if not answer.is_file():
            waiting.append(request["blind_id"])
            continue
        report = report_by_id[request["report_id"]]
        evidence = EvidenceBundle.model_validate(_read(session_dir / "evidence" / f"{report.report_id}.json"))
        if _hash(assessment_messages(report_text(report), evidence)) != request["request_sha256"]:
            raise ValueError(f"Exported request no longer matches frozen inputs: {request['blind_id']}")
        await assess_report(report, evidence,
                            _ExternalAnswer(answer.read_text(encoding="utf-8"), assessor, answer), store)
    if waiting:
        print(f"Awaiting {len(waiting)} answers: {', '.join(waiting)}", file=sys.stderr)
    return summarize_run(session_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paired VA process-report experiment")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run"):
        commands.add_parser(name).add_argument("--config", type=Path, required=True)
    existing = commands.add_parser("assess-existing")
    existing.add_argument("--run-dir", type=Path, required=True)
    existing.add_argument("--config", type=Path, required=True)
    commands.add_parser("summarize").add_argument("--run-dir", type=Path, required=True)
    commands.add_parser("export-assessments").add_argument("--run-dir", type=Path, required=True)
    imported = commands.add_parser("import-assessments")
    imported.add_argument("--session-dir", type=Path, required=True)
    imported.add_argument("--assessor", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            config = load_config(args.config)
            validate_config(config)
            print(f"Valid configuration: {len(config.task_ids)} tasks; no model requests")
        elif args.command == "run":
            print(asyncio.run(run_experiment(load_config(args.config))))
        elif args.command == "summarize":
            print(summarize_run(args.run_dir))
        elif args.command == "export-assessments":
            print(export_assessments(args.run_dir))
        elif args.command == "import-assessments":
            print(asyncio.run(import_assessments(args.session_dir, assessor=args.assessor)))
        else:
            print(asyncio.run(assess_existing(args.run_dir, **load_assessment_config(args.config))))
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
