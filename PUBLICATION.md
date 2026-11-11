# Published pilot snapshot

This repository is a clean publication snapshot for the VA transfer pilot, based on the local SHADE-Arena-derived implementation at 18b9e51c1c6fbca9eec150c7b41644330b6623d6. Upstream source: https://github.com/jkutaso/SHADE-Arena . The upstream license is retained in LICENSE.

The first attempted history push was rejected by GitHub secret scanning for Stripe-shaped values in upstream history and synthetic tests. Push protection was not bypassed. This published repository starts a fresh history and replaces those values (including negative-test copies) with short, intentionally non-credential simulation strings. No source credential values or replacement mapping are included. The original local checkout/history is retained separately for provenance.

Publication data version: va-public-pilot-v1. Modified fixture data: universe_related_stuff/cloud_drive/cloud_drive.yaml; the corresponding solution/reference literal in task_pairs/api_key_calendar_agendas_2/task_definition.py is replaced consistently. These substitutions do not change the normal user task or clean goal text. Transfer task instructions, balances, transfers, recipients and scoring thresholds are unchanged. Semantic fixtures use the same replacement consistently. Golden source/data fingerprints are regenerated for this published dataset; do not pool its data version silently with the original snapshot.

Model IDs and API keys are not supplied. Start with docs/va-bank-pilot.md and configs/va_bank_pilot.example.yaml. No real model run has been started by publishing.

Historical commit IDs in the design/review documents refer to the retained local development history, not ancestors of this clean published repository. The synthetic key lengths differ from the original fixture, so future API-key-task results must not be presented as an unchanged original SHADE run. The current pilot is the transfer task.

## Publication verification

The sanitized snapshot was checked offline using the existing Python3.13 environment: `python -m pytest tests/va/test_config_prompts.py tests/va/test_experiment_cli.py tests/va/test_semantic_outcomes.py tests/va/test_task_rules.py -q` —146 passed,8 existing Pydantic warnings,313.63s. Five normal-task and clean-goal hashes match the retained source checkout; differences beyond Git line endings are limited to the published README, credential fixture/reference substitutions, semantic tests, refreshed golden manifests, and this publication note. No real model/API call or pilot was run.
