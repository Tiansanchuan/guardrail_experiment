# Final fix wave — one scoped re-review

Date: 2026-11-10. Reviewer: `/root/va_final_rereview`.

Scope: `0b72bee53fbc33c15ed378911f19c2232beed8d9..877502de401ff68a1d78fe400e385715c727731e`, exactly the nine paths in the review package. Read `task-final-fix-review-877502d.txt`, `final-findings.md`, `final-fix-report.md`, relevant spec and source. Parent document changes are excluded. No source edits and no broad test run.

## Original findings

1. **ADDRESSED.** Semantic requests/config/rubric hash are durable before calls; raw responses and error envelopes are journaled. Cancellation records an unknown diagnostic, propagates and stops subsequent jobs.
2. **ADDRESSED.** Analysis stops at the first valid report even when its assessment is unavailable; it no longer samples a later actor answer based on assessor success. Later valid assessments of that same report remain selectable.
3. **ADDRESSED.** Spam/travel findings now have actual/attempt/unknown classifications and canonical action/evidence linkage. Frozen transitions preserve reversed/cancelled observed actions; failed requests do not imply an effect; final-state task outcomes remain separate. Travel AA preference does not prove a violated constraint.
4. **ADDRESSED with residual validation gap below.** Expense sharing is initially an observation; independent material-specific protected-content labels control actual leakage and attempts, separately from side quality. Request-only material cannot establish actual exposure or side success. Invalid/missing/unsupported labels and the tested full-side-pass contradiction become unknown/review. Harmless, partial and mixed cases have covering regressions.

## New breakage in the fix diff

**P2 — Identical content can receive contradictory accepted sensitivity labels, producing confident absence of a real exposure.**

`file_content_cases` (semantic_assessment.py:129+) emits both a `shared-*` case and a `request-*` case for a successful share. These represent the same file ID and identical content, but the new validation (around lines 346–367) validates each judgment independently. With side-item scores zero, it accepts `shared=absent` and `request=present` for the same material; `apply_file_content` then records actual violation absent and attempt present. This is an internal contradiction, not merely an unsupported semantic judgment: changing whether the file was actually shared cannot change the sensitivity of identical bytes. A supported request-content judgment can therefore coexist with a confident denial of the resulting real exposure.

Offline reproduction run in this review: construct one `expense_side` job with two content cases, `observation=shared/request`, both artifact `{id_: same-file, content: "Venmo notifications start at $5."}`, both citing `e1` and `action-e1`. Return all completion items score 0; shared protected label absent, request protected label present, both with valid case refs. Use a local scripted ModelReply and complete-trace TaskResult. Observed:

```json
{"assessment_status":"fail","review_required":false,"actual":"absent","attempted":true}
```

The contradiction should be rejected into unknown/review, or identical material should have one independent content judgment with observations tracked separately. This affects actual-behavior strata and report admission/false-admission labels. Parent must adjudicate this residual under the final-wave breaker; this reviewer does not dispatch another fix wave.

## Verification boundary

- Scoped `git diff --check 0b72bee 877502d` passed; HEAD was `877502de401ff68a1d78fe400e385715c727731e`.
- Only the small offline contradiction reproducer was executed by this reviewer; no SDK/API calls or broad suite.
- Parent reports fresh post-finding4 checks: four affected modules 122 passed/8 existing warnings (47.66s), registry/source checks 3 passed/35 deselected (2.50s). These are parent-run results, not tests repeated here.
- The earlier full 279 tests predate finding4; no claim of a post-finding4 full-suite run.

Verdict: four original findings addressed; one residual P2 in the fix diff remains for parent adjudication. Semantic truth and annotation reliability still require audit beyond schema/location checks.

## Parent final adjudication

The four original findings were addressed by 877502d. Parent adjudicated the residual P2 as load-bearing and authorized the original implementer to add the smallest guard, despite the skill final-wave cap. Commit d8895af changes only semantic_assessment.py (9 added lines) and its tests. Parent personally reviewed this exact delta: identical artifact ID/content cannot receive different protected-content statuses; conflicts become unknown/review, with no truth inference. Changed content and distinct artifacts retain separate judgments. Parent fresh complete semantic module: 60 passed, 8 known warnings, 26.78s. Residual closed by parent final review; no second independent scoped review is claimed.
