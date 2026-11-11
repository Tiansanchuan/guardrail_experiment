"""Descriptive paired VA summaries over frozen execution/report artifacts."""

from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path
from statistics import mean

from va_experiment.metrics import METRICS_VERSION, report_metrics
from va_experiment.models import AssessmentRecord, ExecutionRecord, ReportRecord, TaskResult


def _mean(values):
    values = [value for value in values if value is not None]
    return mean(values) if values else None


def _unique(records, field):
    indexed = {}
    for record in records:
        key = getattr(record, field)
        if key in indexed and indexed[key] != record:
            raise ValueError(f"conflicting records for {field}={key}")
        indexed.setdefault(key, record)
    return indexed


def _model(execution):
    config = execution.manifest.get("config", {})
    actor = (config.get("actor") if isinstance(config, dict) else None)
    if actor is None:
        actor = execution.manifest.get("actor", {})
    if not isinstance(actor, dict):
        actor = {}
    return actor.get("provider") or "unknown", actor.get("model_id") or "unknown"


def _rates(statuses):
    counts = Counter(statuses)
    known = counts["pass"] + counts["fail"]
    total = sum(counts.values())
    return {"pass": counts["pass"], "fail": counts["fail"], "unknown": counts["unknown"],
            "known": known, "all_assigned": total,
            "pass_rate_known": counts["pass"] / known if known else None,
            "pass_rate_all_assigned": counts["pass"] / total if total else None}


def _outcomes(executions, results):
    main, side, joint, violations, attempts = [], [], [], [], []
    for execution in executions:
        result = results.get(execution.execution_id)
        m = result.main.status if result else "unknown"
        s = result.side_outcome_observed.status if result else "unknown"
        main.append(m)
        side.append(s)
        joint.append("unknown" if "unknown" in (m, s) else "pass" if m == s == "pass" else "fail")
        violations.append(result.violation_status if result else "unknown")
        attempt = result.main.components.get("violation_attempted",
            result.side_outcome_observed.components.get("violation_attempted")) if result else None
        attempts.append("present" if attempt is True else "absent" if attempt is False else "unknown")
    violation_counts = {status: violations.count(status) for status in ("present", "absent", "unknown")}
    known = violation_counts["present"] + violation_counts["absent"]
    return {"all_assigned": len(executions), "main": _rates(main), "side": _rates(side),
            "joint": _rates(joint), "violation": {**violation_counts, "known": known,
                "all_assigned": len(executions),
                "present_rate_known": violation_counts["present"] / known if known else None},
            "violation_attempt": {status: attempts.count(status) for status in ("present", "absent", "unknown")}}


def _effects(pairs):
    deltas = {goal: _mean([pair["delta_V"] for pair in pairs if pair["goal_condition"] == goal])
              for goal in ("normal_goal", "side_goal")}
    return {"delta_normal_V": deltas["normal_goal"], "delta_side_V": deltas["side_goal"],
            "interaction_V": (deltas["normal_goal"] - deltas["side_goal"]
                              if all(value is not None for value in deltas.values()) else None),
            "complete_pairs_normal": sum(pair["delta_V"] is not None and
                                         pair["goal_condition"] == "normal_goal" for pair in pairs),
            "complete_pairs_side": sum(pair["delta_V"] is not None and
                                       pair["goal_condition"] == "side_goal" for pair in pairs)}


def _task_equal(tasks):
    return {f"task_equal_{metric}": _mean([task[metric] for task in tasks])
            for metric in ("delta_normal_V", "delta_side_V", "interaction_V")}


def _cell_summary(rows):
    metrics = ("D", "V", "V_over_D", "supported", "contradicted", "insufficient_evidence",
               "key_fact_coverage", "violation_coverage", "answer_characters", "answer_words", "output_tokens")
    summary = {"N": len(rows)}
    for metric in metrics:
        values = [row.get(metric) for row in rows]
        summary[f"mean_{metric}"] = _mean(values)
        summary[f"N_{metric}"] = sum(value is not None for value in values)
    for label in ("disclosure", "denial", "false_admission"):
        counts = Counter(row[f"{label}_status"] for row in rows)
        summary[label] = {status: counts[status] for status in
                          ("present", "absent", "unknown", "not_applicable")}
    return summary


def summarize_experiment(executions: list[ExecutionRecord], results: list[TaskResult],
                         reports: list[ReportRecord], assessments: list[AssessmentRecord]) -> dict:
    """Join by IDs, with chronological input order breaking attempt ties.

    The first accepted assessment under the experiment rubric is used per report.
    Rubric comes from an explicit manifest assessment_rubric_version when supplied,
    otherwise the first accepted assessment defines it. Report attempts use ascending
    attempt_index, then input order; only the first usable report per branch counts.
    Original attempts remain in the output ledger. Review/error/truncated reports
    never contribute zero-valued observations to the primary analysis.
    Selected reports must share one protocol version across branches/executions;
    mixed intervention versions are rejected rather than paired or pooled.
    """
    execution_by_id = _unique(executions, "execution_id")
    executions = list(execution_by_id.values())
    result_by_execution = _unique(results, "execution_id")
    reports = list(_unique(reports, "report_id").values())
    assessments = list(_unique(assessments, "assessment_id").values())
    report_by_id = {report.report_id: report for report in reports}
    explicit_rubrics = {execution.manifest["assessment_rubric_version"] for execution in executions
                        if execution.manifest.get("assessment_rubric_version")}
    if len(explicit_rubrics) > 1:
        raise ValueError("cannot pool different assessment rubric versions")
    rubric = next(iter(explicit_rubrics), None)
    if rubric is None:
        rubric = next((assessment.rubric_version for assessment in assessments
                       if assessment.status == "ok" and not assessment.review_required and
                       assessment.report_id in report_by_id and
                       report_by_id[assessment.report_id].execution_id in execution_by_id and
                       report_by_id[assessment.report_id].status == "ok" and
                       report_by_id[assessment.report_id].reply is not None and
                       report_by_id[assessment.report_id].reply.message.get("content")), None)
    assessment_by_report = {}
    assessment_attempts = []
    for assessment in assessments:
        report = report_by_id.get(assessment.report_id)
        reason = ("orphan" if report is None or report.execution_id not in execution_by_id else
                  "review_or_error" if assessment.status != "ok" or assessment.review_required else
                  "rubric_mismatch" if assessment.rubric_version != rubric else
                  "later_valid_attempt" if assessment.report_id in assessment_by_report else "accepted")
        if reason == "accepted":
            assessment_by_report[assessment.report_id] = assessment
        assessment_attempts.append({"assessment_id": assessment.assessment_id,
                                    "report_id": assessment.report_id, "status": assessment.status,
                                    "rubric_version": assessment.rubric_version,
                                    "selected": False, "selection_reason": reason,
                                    "record": assessment.model_dump(mode="json")})
    rows = []
    pairs = []
    branches = []
    selected_reports, selected_assessments = set(), set()
    for execution in executions:
        provider, model_id = _model(execution)
        identity = {"execution_id": execution.execution_id, "task_id": execution.task_id,
                    "goal_condition": execution.goal_condition, "provider": provider, "model_id": model_id}
        selected = {}
        selected_prefixes = {}
        result = result_by_execution.get(execution.execution_id)
        for protocol in ("ordinary", "va_notice"):
            candidates = sorted((report for report in reports if report.execution_id == execution.execution_id
                                 and report.protocol == protocol), key=lambda report: report.attempt_index)
            reason = "missing_report" if not candidates else "unavailable_report"
            for report in candidates:
                if report.status != "ok" or not report.reply or not report.reply.message.get("content"):
                    continue
                assessment = assessment_by_report.get(report.report_id)
                reason = "unavailable_assessment"
                if assessment is None:
                    # The report sampling rule is independent of assessor success.
                    # Reassess this report; never substitute a later actor answer.
                    break
                # Report metrics do not require known task outcomes; an unscored
                # execution supplies unknown behavior and no key-event inventory.
                if result is None:
                    unknown = {"status": "unknown", "score": None, "method": "rule",
                               "rubric_version": "unavailable", "evidence_refs": [], "components": {}}
                    metric_result = TaskResult(execution_id=execution.execution_id, main=unknown,
                        side_outcome_observed=unknown, violation_status="unknown", findings=[],
                        legacy={}, semantic_job_ids=[], key_events=[])
                else:
                    metric_result = result
                selected[protocol] = {**identity, "report_id": report.report_id,
                    "assessment_id": assessment.assessment_id, "protocol": protocol,
                    "behavior_stratum": metric_result.violation_status,
                    **report_metrics(assessment, report, metric_result)}
                selected_reports.add(report.report_id)
                selected_assessments.add(assessment.assessment_id)
                selected_prefixes[protocol] = report.prefix_fingerprint
                reason = "available"
                break
            branches.append({**identity, "protocol": protocol, "reason": reason,
                             "report_attempts": len(candidates)})
        rows.extend(selected.values())
        complete = len(selected) == 2 and len(set(selected_prefixes.values())) == 1
        pairs.append({**identity, "behavior_stratum": result.violation_status if result else "unknown",
                      "ordinary_report_id": selected.get("ordinary", {}).get("report_id"),
                      "va_notice_report_id": selected.get("va_notice", {}).get("report_id"),
                      "ordinary_protocol_version": selected.get("ordinary", {}).get("protocol_version"),
                      "va_notice_protocol_version": selected.get("va_notice", {}).get("protocol_version"),
                      "ordinary_V": selected.get("ordinary", {}).get("V"),
                      "va_notice_V": selected.get("va_notice", {}).get("V"),
                      "delta_V": selected["va_notice"]["V"] - selected["ordinary"]["V"] if complete else None,
                      "pair_status": "complete" if complete else
                          "prefix_mismatch" if len(selected) == 2 else "incomplete"})
    protocol_versions = {row["protocol_version"] for row in rows}
    if len(protocol_versions) > 1:
        raise ValueError("cannot pair or pool different report protocol versions: "
                         + ", ".join(sorted(protocol_versions)))
    protocol_version = next(iter(protocol_versions), None)
    for attempt in assessment_attempts:
        attempt["selected"] = attempt["assessment_id"] in selected_assessments
    tasks = []
    cell_rows = []
    for task_id in sorted({execution.task_id for execution in executions}):
        task_pairs = [pair for pair in pairs if pair["task_id"] == task_id]
        tasks.append({"task_id": task_id, **_effects(task_pairs)})
        for goal in ("normal_goal", "side_goal"):
            for protocol in ("ordinary", "va_notice"):
                selected = [row for row in rows if row["task_id"] == task_id and
                            row["goal_condition"] == goal and row["protocol"] == protocol]
                cell_rows.append({"task_id": task_id, "goal_condition": goal, "protocol": protocol,
                                  **_cell_summary(selected)})
    models = []
    for provider, model_id in sorted({_model(execution) for execution in executions}):
        model_executions = [execution for execution in executions if _model(execution) == (provider, model_id)]
        model_pairs = [pair for pair in pairs if (pair["provider"], pair["model_id"]) == (provider, model_id)]
        model_rows = [row for row in rows if (row["provider"], row["model_id"]) == (provider, model_id)]
        model_tasks = [{"task_id": task_id, **_effects([pair for pair in model_pairs if pair["task_id"] == task_id])}
                       for task_id in sorted({execution.task_id for execution in model_executions})]
        model_cells = [{**cell, **_cell_summary([row for row in model_rows if
                       row["task_id"] == cell["task_id"] and row["goal_condition"] == cell["goal_condition"]
                       and row["protocol"] == cell["protocol"]])} for cell in cell_rows
                       if cell["task_id"] in {execution.task_id for execution in model_executions}]
        models.append({"provider": provider, "model_id": model_id, "tasks": model_tasks,
                       "cells": model_cells, "outcomes": _outcomes(model_executions, result_by_execution),
                       **_task_equal(model_tasks),
                       "pooled_effects": _effects(model_pairs)})
    behavior_strata = []
    for behavior in ("present", "absent", "unknown"):
        behavior_pairs = [pair for pair in pairs if pair["behavior_stratum"] == behavior]
        behavior_strata.append({"violation_status": behavior, "N_executions": len(behavior_pairs),
                                **_effects(behavior_pairs)})
    reasons = Counter(branch["reason"] for branch in branches)
    return {"schema_version": "va-analysis-v1", "rubric_version": rubric, "metrics_version": METRICS_VERSION,
            "protocol_version": protocol_version,
            "analysis_population": "goal_assigned", "tasks": tasks, "cells": cell_rows,
            "models": models, "pairs": pairs, "report_metrics": rows, "branches": branches,
            "assessment_attempts": assessment_attempts,
            "report_attempts": [{"report_id": report.report_id, "execution_id": report.execution_id,
                "protocol": report.protocol, "status": report.status, "attempt_index": report.attempt_index,
                "protocol_version": report.protocol_version,
                "selected": report.report_id in selected_reports} for report in reports],
            **_task_equal(tasks), "N_tasks": len(tasks),
            "N_tasks_with_interaction": sum(task["interaction_V"] is not None for task in tasks),
            "pooled_interaction_V": _effects(pairs)["interaction_V"],
            "behavior_strata_descriptive": behavior_strata,
            "outcomes": _outcomes(executions, result_by_execution),
            "outcomes_by_goal": {goal: _outcomes([execution for execution in executions if
                execution.goal_condition == goal], result_by_execution) for goal in ("normal_goal", "side_goal")},
            "flow": {"all_assigned": len(executions), "expected_report_branches": 2 * len(executions),
                "usable_single_reports": len(rows), "complete_pairs": sum(pair["delta_V"] is not None for pair in pairs),
                "incomplete_pairs": sum(pair["delta_V"] is None for pair in pairs),
                "missing_report_branches": reasons["missing_report"],
                "unavailable_report_branches": reasons["unavailable_report"],
                "unavailable_assessment_branches": reasons["unavailable_assessment"],
                "report_status_counts": dict(Counter(report.status for report in reports)),
                "assessment_status_counts": dict(Counter(assessment.status for assessment in assessments)),
                "execution_termination_counts": dict(Counter(execution.termination for execution in executions)),
                "missing_task_results": sum(execution.execution_id not in result_by_execution for execution in executions),
                "orphan_reports": sum(report.execution_id not in execution_by_id for report in reports),
                "orphan_results": sum(key not in execution_by_id for key in result_by_execution)}}


def write_summary(summary: dict, output_dir: Path) -> None:
    """Write strict JSON plus paired and descriptive CSV tables without model calls."""
    # Serialize before creating files so non-finite caller data cannot leave a
    # partially written JSON artifact that appears to be a successful export.
    serialized = json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(serialized + "\n", encoding="utf-8")
    tables = {name: summary.get(name, []) for name in
              ("pairs", "tasks", "cells", "report_metrics", "branches", "report_attempts")}
    tables["model_tasks"] = [{"provider": model["provider"], "model_id": model["model_id"], **task}
                             for model in summary.get("models", []) for task in model["tasks"]]
    tables["model_cells"] = [{"provider": model["provider"], "model_id": model["model_id"], **cell}
                             for model in summary.get("models", []) for cell in model["cells"]]
    tables["outcomes"] = [{"scope": "all_assigned", **summary.get("outcomes", {})}]
    tables["outcomes"].extend({"scope": goal, **outcomes} for goal, outcomes in
                              summary.get("outcomes_by_goal", {}).items())
    tables["outcomes"].extend({"scope": "model", "provider": model["provider"], "model_id": model["model_id"],
                               **model["outcomes"]} for model in summary.get("models", []))
    for name, rows in tables.items():
        rows = [{"metrics_version": summary.get("metrics_version"),
                 "protocol_version": summary.get("protocol_version"),
                 "rubric_version": summary.get("rubric_version"), **row} for row in rows]
        keys = list(dict.fromkeys(["metrics_version", "rubric_version", "protocol_version"]
                                 + [key for row in rows for key in row]))
        with (output_dir / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value, ensure_ascii=False, allow_nan=False)
                                 if isinstance(value, (dict, list)) else value for key, value in row.items()})
