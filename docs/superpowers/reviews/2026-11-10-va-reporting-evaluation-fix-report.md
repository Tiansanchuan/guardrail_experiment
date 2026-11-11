# Final combined fix wave report

Date: 2026-11-10. Implementer: `/root/va_final_fix`.

Commit: `877502de401ff68a1d78fe400e385715c727731e`.
Parent/base: `0b72bee53fbc33c15ed378911f19c2232beed8d9`.
One combined wave addresses findings 1–4 in `final-findings.md`; item 4 was appended by the parent while this wave remained active, before this sole commit.

## Scope

Exactly nine authorized paths committed: `va_experiment/semantic_assessment.py`, `analysis.py`, `tasks/spam.py`, `tasks/travel.py`, `tasks/expenditure.py`, `rubrics/task_outcomes.json`, `tests/va/test_semantic_outcomes.py`, `test_analysis.py`, `test_task_rules.py`. Parent-owned spec/plan/review documents remain uncommitted and were not staged. No shared types, actor execution, frozen-record persistence or model-client boundary changed.

## Findings addressed

1. **Durable semantic assessment evidence.** Append an exact request/work-item, public model settings, rubric version/hash and no-tool declaration before calling the model. Journal raw success responses and exception envelopes. Cancellation saves an unknown final diagnostic and propagates immediately without continuing jobs. Existing successful verdict/application APIs remain unchanged.
2. **Report sampling independent of assessor acceptance.** Once the first chronological valid report is found, an unavailable assessment leaves that branch missing; analysis never substitutes a later actor answer. Later accepted assessments of the same report remain allowed under the chosen rubric. Failed reports may still yield to the first valid report; all attempts remain exported.
3. **Spam/travel factual event linkage.** Inspect frozen before/after transitions without executing environment tools. Event-proven harmful actions remain actual after reversal/cancellation, with canonical action IDs and evidence refs. Final-state task/side scoring remains separate. Explicit failed harmful requests stay attempts; incomplete/uncertain cases remain unknown. Spam admissions referencing real adapter findings now pass blind-assessment validation. Travel AA preference alone remains unknown/non-proven and is never treated as a concrete violation. Previously established positive transitions survive a later unavailable snapshot.
4. **Protected expense content separated from sharing.** Frozen target sharing is an observation, initially unknown for protected leakage. The existing task-side work item now independently codes each actual-shared or request-only material using `protected_content` present/absent/unknown labels with case-specific evidence. The four completion quality items remain separate. Clear innocent/empty shared content can establish absence; a supported partial protected disclosure establishes actual violation even when side completion fails; unresolved content remains unknown. Failed sensitive requests can establish attempts and never actual exposure or side success. Mixed materials retain local artifact attribution. Missing, invalid, unsupported or contradictory content annotations produce unknown/review, without repair calls. The unreleased task-outcomes-v1 identifier is retained under parent authorization; changed rubric bytes are hashed in manifests and request journals. The expenditure audit note is corrected.

## Verification

All commands used `.venv-va` Python 3.13 and local scripted clients only. No API/network/paid experiment calls.

- RED: `python -m pytest tests/va/test_analysis.py tests/va/test_semantic_outcomes.py -q -k 'first_valid_report_cannot or semantic_call_errors'` → 3 failed, reproducing report substitution (-95) and missing pre-call/error-envelope durability. GREEN after correction: 3 passed, 63 deselected (0.20s).
- RED adapter regressions: `python -m pytest tests/va/test_task_rules.py -q -k 'spam_reverted or spam_failed_matching or travel_reverted'` → 3 failed (transient spam absent, missing attempt component, transient travel unknown). GREEN: 3 passed, 35 deselected, 8 existing warnings (58.11s).
- A four-module run was started during the first three corrections; its terminal output was not recovered after the agent recovery. **No pass claim is made for that run.** Parent independently verified the unchanged first-three-fix tree: 279 passed, 9 existing warnings (1517.11s), with source/test hash stability. This is parent-supplied verification, before finding 4.
- RED finding4: `python -m pytest tests/va/test_semantic_outcomes.py -q -k 'shared_content_score or failed_share_is_attempt'` → 5 failed because harmless/unknown files were marked actual and failed content-unverified sharing was marked a confident attempt.
- Final finding4 GREEN: `python -m pytest tests/va/test_semantic_outcomes.py -q -k 'shared_content_score or failed_share_is_attempt or deleted_disclosure_retained or sensitive_failed_request or mixed_shared_materials or unresolved_or_inconsistent_content'` → **13 passed, 42 deselected, 8 existing warnings (8.02s)**. Includes empty/innocuous content, partial genuine content with side fail, unknown content, retained deleted sharing, request-only sensitive material, innocent actual share plus failed sensitive request, per-material sensitive attribution, missing labels, fake refs and contradictory side completion.
- Scoped `git diff --check` passed before committing; only existing Git LF/CRLF and inaccessible user-global ignore warnings appeared. No blanket warning suppression.

The parent owns final fresh integration/subset validation and one scoped re-review. The 279-test evidence must not be presented as a full-suite run after finding 4.

## Limits retained

Content sensitivity, quality scoring and atomic factual coding remain assessment-model judgments requiring audit; mechanical checks validate schemas, material/evidence associations and conservative uncertainty, not semantic truth. Read-only frozen artifacts and action IDs are preserved; no intent inference is added. No real provider model IDs, sample sizes or budgets were selected.

## Parent-adjudicated breaker correction

The single scoped rereview found a load-bearing residual: the same stable file ID and identical content could appear as both shared material and share-request material with contradictory sensitivity labels. Parent explicitly authorized a narrow breaker correction, recorded the skill-cap deviation/ruling in the ledger, and retained final personal review. No new agent or broad suite was used.

- Commit: `d8895af1f087bd4cecc1e0d9a67755840b770517`; parent: `877502de401ff68a1d78fe400e385715c727731e`.
- Exactly two paths: `va_experiment/semantic_assessment.py` and `tests/va/test_semantic_outcomes.py`.
- Ten-line parser guard keys cases by stable artifact ID plus identical content. Differing statuses, including confident/unknown disagreement, make the entire judgment unknown/review; no sensitivity is inferred and no repair call occurs. Distinct files or genuinely changed contents retain independent judgments.
- RED: `python -m pytest tests/va/test_semantic_outcomes.py -q -k same_material_content_judgments` → 3 failed, 2 passed, 55 deselected (3.27s). Reproduced absent/present, present/absent and absent/unknown conflicts; consistent present and changed-content controls already passed.
- GREEN: `python -m pytest tests/va/test_semantic_outcomes.py -q -k 'same_material_content_judgments or sensitive_failed_request or mixed_shared_materials or unresolved_or_inconsistent_content'` → **11 passed,49 deselected,8 existing warnings (5.33s)**. Covers exact duplicate successful-share/request material, changed content, genuine failed request plus different innocent actual material, per-material attribution and missing/invalid/contradictory coding.
- Scoped diff check passed. Parent owns fresh final subset verification and final personal review of this tiny correction.
