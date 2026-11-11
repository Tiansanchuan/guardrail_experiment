"""Shared, JSON-serializable contracts without task or provider imports."""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from utils.types import ChatMessage as Message

if TYPE_CHECKING:
    from environments.base_environment import BaseEnvironment
    from utils.functions_runtime import FunctionsRuntime
    from va_experiment.records import RecordStore

SCHEMA_VERSION = "va-experiment-v1"
GoalCondition = Literal["normal_goal", "side_goal"]
ReportProtocol = Literal["ordinary", "va_notice"]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
NonemptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DataModel(BaseModel):
    """Reject silently ignored fields and non-finite JSON numbers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ModelConfig(DataModel):
    provider: Literal["fake", "openai", "anthropic"]
    model_id: NonemptyString
    api_key_env: str
    temperature: float
    max_output_tokens: PositiveCount

    @model_validator(mode="after")
    def validate_credential_reference(self) -> ModelConfig:
        if self.api_key_env == "" and self.provider == "fake":
            return self
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must name an environment variable, not contain a key")
        return self


class RunConfig(DataModel):
    schema_version: Literal["va-experiment-v1"]
    task_ids: Annotated[list[NonemptyString], Field(min_length=1)]
    repetitions_per_goal: PositiveCount
    actor: ModelConfig
    assessment: ModelConfig
    max_actor_turns: PositiveCount
    output_dir: Path
    order_seed: Annotated[int, Field(strict=True)]


class ModelReply(DataModel):
    message: Message
    raw_response: dict[str, Any]
    finish_reason: str
    usage: dict[str, Any]
    request_id: str


class ModelCapabilityError(RuntimeError):
    """The configured provider cannot honor a requested capability."""


class ModelClient(Protocol):
    async def complete(
        self, messages: list[Message], runtime: FunctionsRuntime, *, purpose: str,
    ) -> ModelReply: ...


class ToolEvent(DataModel):
    event_id: str
    call_id: str | None
    tool_name: str
    args: dict[str, Any]
    result: Any
    error: str | None
    before_snapshot_id: str
    after_snapshot_id: str
    message_index: Annotated[int, Field(ge=0)]
    executed: bool


class ExecutionRecord(DataModel):
    execution_id: str
    task_id: str
    goal_condition: GoalCondition
    messages: list[Message]
    tool_schema: list[dict[str, Any]]
    initial_snapshot_id: str
    final_snapshot_id: str
    event_ids: list[str]
    termination: str
    manifest: dict[str, Any]
    fingerprint: str


class Verdict(DataModel):
    status: Literal["pass", "fail", "unknown"]
    score: float | None
    method: Literal["rule", "assessment_agent", "human"]
    rubric_version: str
    evidence_refs: list[str]
    components: dict[str, Any]


class TaskResult(DataModel):
    execution_id: str
    main: Verdict
    side_outcome_observed: Verdict
    violation_status: Literal["present", "absent", "unknown"]
    findings: list[dict[str, Any]]
    legacy: dict[str, Any]
    semantic_job_ids: list[str]
    key_events: list[dict[str, Any]]


class ReportRecord(DataModel):
    report_id: str
    execution_id: str
    protocol: ReportProtocol
    protocol_version: str
    request: str
    reply: ModelReply | None
    status: Literal[
        "ok", "empty", "refused", "truncated", "tool_request", "context_limit", "model_error",
    ]
    attempt_index: Annotated[int, Field(ge=0)]
    prefix_fingerprint: str
    error: dict[str, Any] | None


class EvidenceBundle(DataModel):
    blind_id: str
    normal_task: str
    events: list[ToolEvent]
    snapshots: dict[str, dict[str, Any]]
    reference_facts: dict[str, Any]
    key_events: list[dict[str, Any]]


class Claim(DataModel):
    claim_id: str
    canonical_fact: str
    source_start: Annotated[int, Field(ge=0)]
    source_end: Annotated[int, Field(ge=0)]
    task_related: bool
    protocol_meta: bool
    verifiable: bool
    evidence_status: str
    evidence_refs: list[str]
    key_event_refs: list[str]

    @model_validator(mode="after")
    def validate_span(self) -> Claim:
        if self.source_end < self.source_start:
            raise ValueError("source_end must be at or after source_start (exclusive end)")
        return self


class AssessmentRecord(DataModel):
    assessment_id: str
    blind_id: str
    report_id: str
    claims: list[Claim]
    disclosure: dict[str, Any]
    denial: dict[str, Any]
    false_admission: dict[str, Any]
    review_required: bool
    raw_reply: ModelReply | None
    rubric_version: str
    status: str


class SemanticJob(DataModel):
    job_id: str
    kind: Literal["task_main", "task_side", "recover_key"]
    normal_task: str
    rubric: str
    evidence: dict[str, Any]
    input_fingerprint: str


class TaskAdapter(Protocol):
    task_id: str
    normal_task: str
    clean_side_goal: str

    def create_environment(self) -> BaseEnvironment: ...

    def restore_environment(self, snapshot: dict) -> BaseEnvironment: ...

    def score_execution(self, record: ExecutionRecord, store: RecordStore) -> TaskResult: ...

    def semantic_jobs(
        self, record: ExecutionRecord, result: TaskResult, store: RecordStore,
    ) -> list[SemanticJob]: ...
