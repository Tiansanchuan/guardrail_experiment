"""Hand-computed, offline checks of the public analysis interfaces."""

import csv
import json

import pytest

from va_experiment.analysis import summarize_experiment, write_summary
from va_experiment.metrics import report_metrics
from va_experiment.models import Claim, TaskResult, Verdict


def verdict(status="pass"):
    return Verdict(status=status, score=None, method="rule", rubric_version="test-v1",
                   evidence_refs=[], components={})


def outcome(execution_id, *, main="pass", side="fail", violation="absent", key_events=()):
    return TaskResult(execution_id=execution_id, main=verdict(main),
                      side_outcome_observed=verdict(side), violation_status=violation,
                      findings=[], legacy={}, semantic_job_ids=[], key_events=list(key_events))


def claims(count):
    return [Claim(claim_id=f"c{i}", canonical_fact=f"Fact {i}", source_start=0, source_end=1,
                  task_related=True, protocol_meta=False, verifiable=True,
                  evidence_status="supported", evidence_refs=[f"event-{i}"], key_event_refs=[])
            for i in range(count)]


def cells(stored_execution, report_pair, scored_pair, *, normal=(2, 5), side=(3, 4), task="mini"):
    executions, results, reports, assessments = [], [], [], []
    for goal, values in (("normal_goal", normal), ("side_goal", side)):
        execution = stored_execution(goal).record
        execution.task_id = task
        pair = report_pair(execution.execution_id)
        executions.append(execution)
        results.append(outcome(execution.execution_id))
        reports.extend(pair)
        assessments.extend(scored_pair(*pair, ordinary_claims=claims(values[0]),
                                       va_claims=claims(values[1])))
    return executions, results, reports, assessments


def test_four_cell_interaction(stored_execution, report_pair, scored_pair):
    summary = summarize_experiment(*cells(stored_execution, report_pair, scored_pair))
    task = summary["tasks"][0]
    assert (task["delta_normal_V"], task["delta_side_V"], task["interaction_V"]) == (3, 1, 2)
    assert summary["task_equal_interaction_V"] == 2
    assert summary["outcomes"]["all_assigned"] == 2
    assert summary["cells"][0]["N"] == 1


def test_first_valid_report_cannot_be_replaced_when_its_assessment_fails(
    stored_execution, report_pair, scored_pair,
):
    data = cells(stored_execution, report_pair, scored_pair)
    first = data[2][0]
    data[3][0].status = "needs_review"
    later = first.model_copy(update={"report_id": "later-report", "attempt_index": 1})
    later_assessment = data[3][0].model_copy(update={"assessment_id": "later-assessment",
        "report_id": later.report_id, "status": "ok", "claims": claims(100)})
    data[2].append(later)
    data[3].append(later_assessment)
    summary = summarize_experiment(*data)
    assert summary["tasks"][0]["delta_normal_V"] is None
    assert summary["flow"]["unavailable_assessment_branches"] == 1
    assert not any(row["selected"] for row in summary["report_attempts"]
                   if row["report_id"] == later.report_id)


def test_missing_branch_and_duplicate_attempts_keep_execution_denominators(
    stored_execution, report_pair, scored_pair,
):
    executions, results, reports, assessments = cells(stored_execution, report_pair, scored_pair)
    results[1] = outcome(executions[1].execution_id, main="unknown", side="unknown", violation="unknown")
    later = assessments[0].model_copy(update={"assessment_id": "later", "claims": claims(50)})
    summary = summarize_experiment(executions, results, reports[:3],
                                   [assessments[0], later, *assessments[1:]])
    assert summary["tasks"][0]["delta_normal_V"] == 3
    assert summary["tasks"][0]["delta_side_V"] is None
    assert summary["tasks"][0]["interaction_V"] is None
    assert summary["flow"]["complete_pairs"] == 1
    assert summary["flow"]["missing_report_branches"] == 1
    assert len(summary["assessment_attempts"]) == 5
    assert sum(row["selected"] for row in summary["assessment_attempts"]) == 3
    assert summary["outcomes"]["main"] == {
        "pass": 1, "fail": 0, "unknown": 1, "known": 1, "all_assigned": 2,
        "pass_rate_known": 1, "pass_rate_all_assigned": .5,
    }


def test_task_equal_summary_and_explicit_model_groups(stored_execution, report_pair, scored_pair):
    first = cells(stored_execution, report_pair, scored_pair, normal=(0, 3), side=(0, 1), task="a")
    second = cells(stored_execution, report_pair, scored_pair, normal=(0, 10), side=(0, 2), task="b")
    third = cells(stored_execution, report_pair, scored_pair, normal=(0, 10), side=(0, 2), task="b")
    combined = [first[i] + second[i] + third[i] for i in range(4)]
    for execution in combined[0]:
        execution.manifest["actor"] = {"provider": "fake", "model_id": "explicit-model"}
    summary = summarize_experiment(*combined)
    assert summary["task_equal_interaction_V"] == 5
    assert summary["pooled_interaction_V"] == 6
    assert summary["models"][0]["model_id"] == "explicit-model"
    assert summary["models"][0]["task_equal_interaction_V"] == 5


def test_review_and_failed_reports_are_missing_not_zero(stored_execution, report_pair, scored_pair):
    executions, results, reports, assessments = cells(stored_execution, report_pair, scored_pair)
    assessments[1].status = "needs_review"
    reports[3].status = "truncated"
    summary = summarize_experiment(executions, results, reports, assessments)
    assert summary["flow"]["complete_pairs"] == 0
    assert summary["flow"]["unavailable_assessment_branches"] == 1
    assert summary["flow"]["unavailable_report_branches"] == 1
    assert summary["task_equal_interaction_V"] is None
    assert summary["outcomes"]["all_assigned"] == 2
    assert summary["flow"]["report_status_counts"]["truncated"] == 1


def test_metrics_deduplicate_facts_and_separate_verifiability_from_truth(report_pair, scored_pair):
    ordinary, va = report_pair("execution")
    annotations = claims(4)
    annotations[0].evidence_status = "contradicted"
    annotations[1].evidence_status = "insufficient_evidence"
    annotations[2].task_related = False
    annotations[3].protocol_meta = True
    duplicate = annotations[0].model_copy(update={"claim_id": "duplicate", "canonical_fact": "  FACT   0 "})
    assessment, _ = scored_pair(ordinary, va, ordinary_claims=[*annotations, duplicate])
    metrics = report_metrics(assessment, ordinary, outcome("execution"))
    assert (metrics["D"], metrics["V"], metrics["V_over_D"]) == (3, 2, 2 / 3)
    assert (metrics["supported"], metrics["contradicted"], metrics["insufficient_evidence"]) == (1, 1, 1)
    assert metrics["key_fact_coverage"] is None
    assert metrics["violation_coverage"] is None


def test_coverage_uses_unique_key_event_inventory_and_supported_refs(report_pair, scored_pair):
    ordinary, va = report_pair("execution")
    annotations = claims(3)
    annotations[0].key_event_refs = ["action-a", "action-a", "bogus"]
    annotations[1].key_event_refs = ["action-b"]
    annotations[1].evidence_status = "contradicted"
    annotations[2].evidence_refs = []
    annotations[2].key_event_refs = ["action-b"]
    assessment, _ = scored_pair(ordinary, va, ordinary_claims=annotations)
    result = outcome("execution", violation="present", key_events=[
        {"key_event_id": "action-a"}, {"key_event_id": "action-a"}, {"key_event_id": "action-b"},
    ])
    result.findings = [{"classification": "actual_violation", "key_event_refs": ["action-a", "action-b"]}]
    metrics = report_metrics(assessment, ordinary, result)
    assert (metrics["key_event_count"], metrics["key_events_covered"], metrics["key_fact_coverage"]) == (2, 1, .5)
    assert metrics["violation_coverage"] == .5
    assert (metrics["supported"], metrics["contradicted"], metrics["insufficient_evidence"]) == (1, 1, 1)
    result.key_events.append({"event_id": "missing-key-id"})
    assert report_metrics(assessment, ordinary, result)["key_fact_coverage"] is None


def test_labels_follow_observed_behavior_and_require_supporting_claims(report_pair, scored_pair):
    ordinary, va = report_pair("execution")
    annotations = claims(2)
    annotations[1].evidence_status = "contradicted"
    assessment, _ = scored_pair(ordinary, va, ordinary_claims=annotations)
    assessment.disclosure = {"status": "present", "claim_refs": ["c0"], "evidence_refs": ["event-0"]}
    assessment.denial = {"status": "present", "claim_refs": ["c1"], "evidence_refs": ["event-1"]}
    assessment.false_admission = assessment.denial.copy()
    present = report_metrics(assessment, ordinary, outcome("execution", violation="present"))
    assert (present["disclosure_status"], present["denial_status"], present["false_admission_status"]) == (
        "present", "present", "not_applicable")
    absent = report_metrics(assessment, ordinary, outcome("execution", violation="absent"))
    assert (absent["disclosure_status"], absent["denial_status"], absent["false_admission_status"]) == (
        "not_applicable", "not_applicable", "present")
    unknown = report_metrics(assessment, ordinary, outcome("execution", violation="unknown"))
    assert (unknown["disclosure_status"], unknown["denial_status"], unknown["false_admission_status"]) == (
        "unknown", "unknown", "unknown")
    assessment.false_admission["claim_refs"] = ["nonexistent"]
    assert report_metrics(assessment, ordinary, outcome("execution"))["false_admission_status"] == "unknown"


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_unknown_experiment_exports_nulls(tmp_path, stored_execution, empty):
    executions = [] if empty else [stored_execution().record]
    summary = summarize_experiment(executions, [], [], [])
    assert summary["task_equal_interaction_V"] is None
    assert summary["outcomes"]["main"]["pass_rate_known"] is None
    assert summary["outcomes"]["main"]["unknown"] == (0 if empty else 1)
    write_summary(summary, tmp_path)
    text = (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["task_equal_interaction_V"] is None
    with (tmp_path / "pairs.csv").open(encoding="utf-8", newline="") as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == (0 if empty else 1)


def test_model_metadata_uses_execution_manifest_config(stored_execution, report_pair, scored_pair):
    data = cells(stored_execution, report_pair, scored_pair)
    data[0][0].manifest["config"] = {"actor": {"provider": "fake", "model_id": "actual-shape"}}
    summary = summarize_experiment(*data)
    assert {model["model_id"] for model in summary["models"]} == {"actual-shape", "unknown"}


def test_first_valid_assessment_under_explicit_rubric_and_first_report_attempt_wins(
    stored_execution, report_pair, scored_pair,
):
    data = cells(stored_execution, report_pair, scored_pair)
    data[0][0].manifest["assessment_rubric_version"] = "fixture-v1"
    rejected = data[3][0].model_copy(update={"assessment_id": "rejected", "status": "parse_error"})
    wrong_rubric = data[3][0].model_copy(update={"assessment_id": "wrong-rubric", "rubric_version": "v2",
                                              "claims": claims(100)})
    earlier = data[3][0].model_copy(update={"assessment_id": "first-valid", "claims": claims(1)})
    late_report = data[2][0].model_copy(update={"report_id": "later-report", "attempt_index": 1})
    late_assessment = data[3][0].model_copy(update={"assessment_id": "late-report-assessment",
                                                 "report_id": "later-report", "claims": claims(100)})
    data[2].insert(0, late_report)  # Input order cannot override report attempt_index.
    data[3][:0] = [late_assessment, rejected, wrong_rubric, earlier]
    summary = summarize_experiment(*data)
    assert summary["tasks"][0]["delta_normal_V"] == 4
    selected = {attempt["assessment_id"] for attempt in summary["assessment_attempts"] if attempt["selected"]}
    assert "first-valid" in selected
    assert not selected & {"rejected", "wrong-rubric", "late-report-assessment"}
    assert len(summary["report_metrics"]) == 4


def test_duplicate_ids_cannot_double_outcomes_and_conflicts_are_rejected(stored_execution, report_pair, scored_pair):
    data = cells(stored_execution, report_pair, scored_pair)
    data[0].append(data[0][0].model_copy(deep=True))
    data[1].append(data[1][0].model_copy(deep=True))
    data[2].append(data[2][0].model_copy(deep=True))
    data[3].append(data[3][0].model_copy(deep=True))
    summary = summarize_experiment(*data)
    assert summary["outcomes"]["all_assigned"] == 2
    assert summary["outcomes"]["main"]["pass"] == 2
    assert len(summary["report_metrics"]) == 4
    data[0][-1].termination = "different_history"
    with pytest.raises(ValueError, match="conflicting records"):
        summarize_experiment(*data)


def test_frozen_outcomes_do_not_depend_on_report_protocol_or_failures(stored_execution, report_pair, scored_pair):
    data = cells(stored_execution, report_pair, scored_pair)
    data[1][0] = outcome(data[0][0].execution_id, side="pass", violation="present")
    data[1][0].main.components["violation_attempted"] = True
    before = [record.model_dump(mode="json") for records in data for record in records]
    full = summarize_experiment(*data)
    ordinary_only = summarize_experiment(data[0], data[1], data[2][::2], data[3])
    assert full["outcomes"] == ordinary_only["outcomes"]
    assert full["outcomes"]["violation"] == {
        "present": 1, "absent": 1, "unknown": 0, "known": 2,
        "all_assigned": 2, "present_rate_known": .5,
    }
    assert full["outcomes"]["violation_attempt"] == {"present": 1, "absent": 0, "unknown": 1}
    assert [row["N_executions"] for row in full["behavior_strata_descriptive"]] == [1, 1, 0]
    assert before == [record.model_dump(mode="json") for records in data for record in records]


def test_mismatched_frozen_prefix_excludes_pair_and_exports_metrics(tmp_path, stored_execution, report_pair, scored_pair):
    data = cells(stored_execution, report_pair, scored_pair)
    data[2][1].prefix_fingerprint = "different-frozen-history"
    summary = summarize_experiment(*data)
    assert summary["pairs"][0]["pair_status"] == "prefix_mismatch"
    assert summary["tasks"][0]["delta_normal_V"] is None
    write_summary(summary, tmp_path)
    with (tmp_path / "report_metrics.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    assert rows[0]["V"] == "2"
    assert rows[0]["key_fact_coverage"] == ""
    with (tmp_path / "model_tasks.csv").open(encoding="utf-8", newline="") as stream:
        model_tasks = list(csv.DictReader(stream))
    assert model_tasks[0]["model_id"] == "unknown"
    with (tmp_path / "outcomes.csv").open(encoding="utf-8", newline="") as stream:
        outcome_rows = list(csv.DictReader(stream))
    assert outcome_rows[0]["all_assigned"] == "2"
    assert summary["task_equal_delta_side_V"] == 1


def test_malformed_positive_label_and_unavailable_token_usage_remain_unknown(report_pair, scored_pair):
    ordinary, va = report_pair("execution")
    ordinary.reply.usage = {"output_tokens": "unavailable"}
    assessment, _ = scored_pair(ordinary, va, ordinary_claims=claims(1))
    assessment.disclosure = {"status": "present", "claim_refs": [{}], "evidence_refs": ["event-0"]}
    metric = report_metrics(assessment, ordinary, outcome("execution", violation="present"))
    assert metric["disclosure_status"] == "unknown"
    assert metric["output_tokens"] is None


def test_attempt_only_findings_never_enter_actual_violation_coverage(report_pair, scored_pair):
    ordinary, va = report_pair("execution")
    annotations = claims(1)
    annotations[0].key_event_refs = ["action-actual"]
    assessment, _ = scored_pair(ordinary, va, ordinary_claims=annotations)
    result = outcome("execution", violation="present", key_events=[
        {"key_event_id": "action-actual"}, {"key_event_id": "action-attempt"},
    ])
    result.findings = [
        {"classification": "actual_violation", "key_event_refs": ["action-actual"]},
        {"classification": "attempt", "key_event_refs": ["action-attempt"]},
    ]
    metrics = report_metrics(assessment, ordinary, result)
    assert metrics["violation_coverage"] == 1
    assert metrics["violation_key_event_count"] == 1
    result.findings.append({"classification": "actual_violation", "key_event_refs": []})
    assert report_metrics(assessment, ordinary, result)["violation_coverage"] is None


@pytest.mark.parametrize("changed_reports", [(1,), (2, 3)], ids=["within-pair", "across-executions"])
def test_mixed_selected_protocol_versions_cannot_be_paired_or_pooled(
    stored_execution, report_pair, scored_pair, changed_reports,
):
    data = cells(stored_execution, report_pair, scored_pair)
    for index in changed_reports:
        data[2][index].protocol_version = "report-protocol-v2"
    with pytest.raises(ValueError, match="different report protocol versions"):
        summarize_experiment(*data)


def test_selected_protocol_version_is_preserved_in_json_and_csv(
    tmp_path, stored_execution, report_pair, scored_pair,
):
    data = cells(stored_execution, report_pair, scored_pair)
    for report in data[2]:
        report.protocol_version = "report-protocol-v7"
    summary = summarize_experiment(*data)
    assert summary["task_equal_interaction_V"] == 2
    assert summary["protocol_version"] == "report-protocol-v7"
    assert {row["protocol_version"] for row in summary["report_metrics"]} == {"report-protocol-v7"}
    write_summary(summary, tmp_path)
    exported = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert exported["protocol_version"] == "report-protocol-v7"
    for name in ("pairs", "tasks", "cells", "report_metrics", "branches", "report_attempts",
                 "model_tasks", "model_cells", "outcomes"):
        with (tmp_path / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows and {row["protocol_version"] for row in rows} == {"report-protocol-v7"}
