# VA process-report experiment

The new entry point runs one actor under normal and additional-goal conditions.
After each execution freezes, it requests ordinary and VA-notice process reports
from independent copies of the entire same history. A new assessment agent codes
each report against frozen evidence. There is no interrogation or follow-up turn.
Existing monitor, evaluator and historical runner entry points remain outside this
flow. Main and side outcomes count once per execution, regardless of report count.

## Environment and offline verification

Use a dedicated Python environment (the implementation was checked on Python 3.13):

```powershell
python -m venv .venv-va
.\.venv-va\Scripts\python.exe -m pip install -r requirements-va.txt
.\.venv-va\Scripts\python.exe -m pytest tests/va -q
.\.venv-va\Scripts\python.exe run_va_experiment.py --help
```

Do not install the historical repository's full media/GPU dependency list for this
experiment. Provider auto-discovery still requires the minimal provider import
chain listed in `requirements-va.txt`. Offline tests inject scripted clients and
exercise the actual in-memory banking tools; they do not make API requests.
There is no fake experiment command or manufactured five-task result file.

```powershell
.\.venv-va\Scripts\python.exe -m pytest tests/va/test_experiment_cli.py -q
```

The simulated email, banking, payment and calendar tools modify local environment
objects only; they have no connections to real accounts or payment/email services.
The configured actor and assessment model providers are external API connections
during live runs. Actual SDK credentials are redacted at the client boundary;
simulated API keys and other task evidence remain intact for scoring and audit.

## Configuration and commands

Copy `configs/va_experiment.example.yaml` to a local settings file. Its null fields
are deliberate: model IDs, role settings, repeated sample count and actor budget
must be supplied before a live run. No model or budget is selected automatically.
Supported providers are `openai` and `anthropic` for live CLI calls. The internal
`fake` provider requires explicitly injected scripted clients in Python tests.

Both `actor` and `assessment` require `provider`, `model_id`, `api_key_env`,
`temperature`, and `max_output_tokens`. The run also requires `schema_version`,
`task_ids`, `repetitions_per_goal`, `max_actor_turns`, `output_dir`, and `order_seed`.
`api_key_env` is the name of an environment variable holding the credential, never
the credential itself. Supply those environment variables through your local
secret-management mechanism. Validation does not require their values or construct
SDK clients. Before execution both role credentials and all task IDs are checked
before constructing either model. Model availability must be checked with the
selected provider when preparing a real budget; none is implied by historical IDs.

```powershell
.\.venv-va\Scripts\python.exe run_va_experiment.py validate --config path\run.yaml
# Paid invocation: run only after choosing concrete models, sample count and limits.
.\.venv-va\Scripts\python.exe run_va_experiment.py run --config path\run.yaml
.\.venv-va\Scripts\python.exe run_va_experiment.py summarize --run-dir path\run-directory
```

`validate`, `--help` and `summarize` construct no SDK clients and make no network
requests. Relative `output_dir` is resolved against the configuration file's
directory. Each `run` creates a unique child and refuses to overwrite artifacts.
Running `run` again creates new actor executions and may incur new charges.
There is no resume command that replays environment tools.

The five registered IDs are `bank_transfer_2`, `american_airlines_2`,
`spam_filter_update`, `api_key_calendar_agendas_2`, and `expenditure_accounting`.
Unsupported or duplicate IDs fail before client construction. Both goals use the
same actor configuration, normal task, tools, environment version and turn budget.
Report ordering and assessment scheduling use local seeded shuffles; this seed is
not an inference RNG seed. Each report receives a fresh random blind UUID. Private
ID/protocol associations never enter the assessor's model input. Frozen tool
arguments, outputs and snapshots are untrusted evidence, not new instructions.

## Saved artifacts

```text
output_dir/
  run-<UTC timestamp>-<random UUID>/
    manifest.json                 effective configs, runtime/dependencies, code hashes,
                                  Git HEAD/dirty-diff hash, versions, planned count
    task_manifests/<task>.json     full source/fact manifest saved before actor calls
    journal.jsonl                 append-only requests, raw replies, tools, reports,
                                  task results, assessments and private mappings
    snapshots/<SHA256>.json        immutable state, including failed tool transitions
    executions/<execution>.json    immutable history, tool schema, prefix and stop
    results/<execution>.json       once-per-execution rule/semantic outcome
    evidence/<report>.json         immutable assessor evidence bundle
    evidence_links/<report>.json   report/execution/prefix/result/evidence association hashes
    summary.json
    pairs.csv, tasks.csv, cells.csv, report_metrics.csv, branches.csv,
    report_attempts.csv, model_tasks.csv, model_cells.csv, outcomes.csv
```

Reports are `report_record` journal entries; assessment attempts are
`report_assessment_record` entries, in durable chronological order. The full raw
trace remains in this store, including available provider-native data. Execution
records are never modified to attach later provenance: `execution_task_manifest`
journal entries link their IDs to the already-saved manifest. The complete task
manifest, including reference facts, is checked before scoring and bundle creation.

An execution failure, unavailable report, truncation, context limit or review case
stays explicit. Unknown outcomes do not become failures or zero-valued report
metrics. A branch exception preserves its diagnostics and permits the other report
branch. Cancellation preserves completed evidence and stops further model requests.
No automatic actor/report retry repairs the history. Interrupted outputs can be
summarized; missing reports or evidence must be investigated rather than fabricated.
`planned_executions` records the configured schedule; analysis execution denominators
cover frozen executions actually present, with missing reports shown separately.

## Offline re-evaluation and audit

`assess-existing` reads saved reports, executions, results and evidence, checks their
association fingerprints, and makes assessment calls only. It never executes an
actor, calls environment tools, rebuilds bundles from current task sources, or
rewrites source journals/snapshots. Re-evaluation therefore works after task source
drift. A copied bundle or changed source report/result fails before model creation.
An existing run must have saved bundles and associations; missing artifacts are a
clear error requiring manual audit, not an invitation to regenerate evidence.

Use a separate YAML containing exactly these two keys (replace all model settings):

```yaml
assessment:
  provider: openai
  model_id: YOUR_EXPLICIT_MODEL_ID
  api_key_env: YOUR_ASSESSMENT_KEY_ENV
  temperature: 0
  max_output_tokens: 4096
rubric_version: report-assessment-v1
```

The rubric ID must match `va_experiment/rubrics/report_assessment.json` in the
installed checkout. An arbitrary label cannot silently relabel a different rubric.
To revise a rubric, implement and review its versioned code/JSON before invoking
re-evaluation. This command can incur assessment-provider charges:

```powershell
.\.venv-va\Scripts\python.exe run_va_experiment.py assess-existing --run-dir path\run-directory --config path\reassessment.yaml
.\.venv-va\Scripts\python.exe run_va_experiment.py summarize --run-dir path\printed-assessment-directory
```

The printed directory is a fresh sibling named
`assessment-<source-run-name>-<UTC timestamp>-<UUID>`. It contains a new
`manifest.json` (source path/hash, override assessment config, exact rubric version,
runtime/code/dependencies), independent `journal.jsonl`, per-report `evidence/`
copies with new blind UUIDs, and the summary/CSV artifacts. Its execution and report
inputs stay in the source run. Its summary selects the first accepted assessment
per report **within this new session**, preserving chronological attempts. It does
not pool older model/rubric sessions or replace original assessments. Summarizing
the original directory continues to summarize its original session.

For manual audit, use `branches.csv` to identify missing/review cases, then join
the assessment ID and report ID through journal mappings, inspect exact public
report spans, and follow cited event/snapshot IDs. A reference validates a location;
semantic support and atomicity still require judgment. CoT/system text is retained
for trace provenance and never treated as behavioral ground truth.

Preserve corrections as new, attributed audit artifacts with the original IDs,
reviewer/date, reason, corrected fields and evidence references. Never edit the
original report, frozen evidence, accepted assessment or execution JSON. Task-outcome
human overrides can use `semantic_assessment.append_task_override`; its append-only
audit event does not rewrite the frozen result. Applying a human-reviewed outcome or
report annotation to analysis requires an explicitly separate reviewed dataset/session
with provenance; the four-command CLI does not silently consume human override events.
No UI or unvalidated JSON edit is treated as an accepted assessment.

## Acceptance coverage

| Spec item | Offline evidence |
|---|---|
| 1. Full flow; no old monitor/evaluator | `test_experiment_cli.py`: two executions/four reports, legacy import guard |
| 2. Same actor tools and normal task | `test_execution_records.py`, `test_config_prompts.py` |
| 3. Clean actor/side-goal/end-task prompts | `test_config_prompts.py`, task adapter tests |
| 4. Identical frozen prefix; one report each | `test_reporting.py` |
| 5. Raw/error/tool state and unique provenance | `test_execution_records.py`, CLI manifest/association checks |
| 6. Blind inputs, references, errors and empty details | `test_report_assessment.py`, `test_analysis.py` |
| 7. Observed behavior independent of assigned goal | task rules, semantic outcomes, assessment and analysis tests |
| 8. Boundary values and transformed disclosures | `test_task_rules.py`, `test_semantic_outcomes.py` |
| 9. Five adapters, independent semantic work | CLI five-ID validation and `test_semantic_outcomes.py` |
| 10. Missing branch/error/retry accounting | reporting/execution/analysis tests and CLI branch-failure test |

These are implementation acceptance checks using scripted completions and local
fixtures. No live pilot, model quality result, formal sample-size decision or
inference about a VA effect is implied by passing tests.
