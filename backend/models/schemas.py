from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ArtifactType(str, Enum):
    """Deployable or consumable output produced by a repository subtree."""

    LIBRARY = "library"
    FRAMEWORK = "framework"
    CLI_TOOL = "cli_tool"
    DEPLOYABLE_SERVICE = "deployable_service"
    WEB_APPLICATION = "web_application"
    DESKTOP_APPLICATION = "desktop_application"
    MOBILE_APPLICATION = "mobile_application"
    PLUGIN = "plugin"
    DATA_PIPELINE = "data_pipeline"
    DOCUMENTATION = "documentation"
    UNKNOWN = "unknown"


class ArtifactCount(str, Enum):
    SINGLE = "single"
    MULTI = "multi"


class ArchitecturalLayerName(str, Enum):
    """Functional tier occupied by a technology within an artifact."""

    FRONTEND = "frontend"
    BACKEND = "backend"
    MESSAGING = "messaging"
    CACHE = "cache"
    DATA = "data"
    OBSERVABILITY = "observability"
    INFRA = "infra"
    TESTING = "testing"
    AI_ML = "ai_ml"
    LANGUAGE_RUNTIME = "language_runtime"


class AssignmentMethod(str, Enum):
    """Provenance of a resolved architectural-layer assignment."""

    DETERMINISTIC = "deterministic"
    AI_INFERRED = "ai_inferred"
    EMERGENT = "emergent"
    PROVISIONAL = "provisional"
    MAINTAINER_APPROVED = "maintainer_approved"


class UsageScope(str, Enum):
    RUNTIME = "runtime"
    DEV = "dev"
    TEST = "test"
    BUILD = "build"


class Artifact(BaseModel):
    name: str = Field(min_length=1)
    type: ArtifactType
    path: str = Field(min_length=1)
    primary: bool = False
    subordinate_to: Optional[str] = None


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewItemKind(str, Enum):
    CORRECTION = "correction"
    CLASSIFIER_DIAGNOSIS = "classifier_diagnosis"
    EMERGENT_TYPE = "emergent_type"
    EMERGENT_ROLE = "emergent_role"


class ReviewSource(str, Enum):
    USER = "user"
    AI_PIPELINE = "ai_pipeline"
    MAINTAINER = "maintainer"
    UNKNOWN = "unknown"


class ReviewItem(BaseModel):
    id: str
    kind: ReviewItemKind
    status: ReviewStatus = ReviewStatus.PENDING
    pipeline_value: str
    proposed_value: str | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    assignment_method: str | None = None
    source: ReviewSource | str = ReviewSource.USER
    seen_count: int = 0
    created_by: str | None = None
    reviewed_by: str | None = None
    created_at: str | None = None
    reviewed_at: str | None = None
    note: str | None = None


class ReviewActionRequest(BaseModel):
    target_value: str | None = None
    merge_into: str | None = None
    merge_target: str | None = None
    note: str | None = None


class ReviewDecisionResponse(BaseModel):
    item_id: str
    status: ReviewStatus
    kind: ReviewItemKind
    message: str


class RepositoryClassification(BaseModel):
    """
    Stage-1 structural output.

    Semantic fields such as software_type remain additive until the deliberate
    schema-version/re-embedding migration. Artifact ownership can already rely
    on this deterministic portion of the contract.
    """

    artifact_count: ArtifactCount
    artifacts: list[Artifact] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_artifact_graph(self):
        names = [artifact.name for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError("artifact names must be unique")

        expected = ArtifactCount.SINGLE if len(names) == 1 else ArtifactCount.MULTI
        if self.artifact_count != expected:
            raise ValueError(
                f"artifact_count must be '{expected.value}' for {len(names)} artifact(s)"
            )

        primaries = [artifact for artifact in self.artifacts if artifact.primary]
        if len(primaries) != 1:
            raise ValueError("exactly one artifact must be primary")

        known = set(names)
        for artifact in self.artifacts:
            parent = artifact.subordinate_to
            if parent is not None and parent not in known:
                raise ValueError(
                    f"artifact '{artifact.name}' references unknown parent '{parent}'"
                )
            if parent == artifact.name:
                raise ValueError("an artifact cannot be subordinate to itself")
        return self


class ArchitecturalLayer(BaseModel):
    # String-backed so maintainer-approved emergent layers remain representable;
    # built-in values still originate from ArchitecturalLayerName.
    primary: str
    secondary: list[str] = Field(default_factory=list)
    assignment_method: AssignmentMethod
    confidence: float = Field(ge=0.0, le=1.0)
    disambiguation_pending: bool = False

    @model_validator(mode="after")
    def validate_layers(self):
        if self.primary in self.secondary:
            raise ValueError("primary layer cannot also be a secondary layer")
        if len(self.secondary) != len(set(self.secondary)):
            raise ValueError("secondary layers must be unique")
        return self


class DetectedTech(BaseModel):
    name: str
    confidence: float
    detection_source: str
    version: str | None = None
    technology_role: str
    scope: str | None = None
    origin: str | None = None
    matched_file: str | None = None
    version_spec: str | None = None
    manifest_frequency: int | None = None
    file_count: int | None = None
    emergent_technology_role: str | None = None
    byte_count: Optional[int] = None
    byte_share: Optional[float] = None
    assignment_method: Optional[str] = None
    multi_role: bool = False
    secondary_roles: list[str] = Field(default_factory=list)
    architectural_layer: Optional[ArchitecturalLayer] = None
    layer_inference_status: Optional[str] = None
    belongs_to_artifact: Optional[str] = None
    usage_scope: Optional[UsageScope] = None


class PatternMatch(BaseModel):
    tech: str
    technology_role: str
    matched_file: str
    matched_keyword: str
    confidence: float
    scope: Optional[str] = None
    origin: Optional[str] = None
    version_spec: Optional[str] = None
    manifest_frequency: Optional[int] = None


class AiInference(BaseModel):
    tech: str
    technology_role: str
    reasoning: str
    confidence: float


class StackAnalysis(BaseModel):
    languages: list[DetectedTech]
    frameworks: list[DetectedTech]
    databases: list[DetectedTech]
    messaging: list[DetectedTech]
    ai_ml: list[DetectedTech]
    infra: list[DetectedTech]
    testing: list[DetectedTech]
    library: list[DetectedTech] = []
    primary_language: str
    complexity_score: int = Field(
        default=1,
        ge=1,
        le=10,
        description=(
            "Detection confidence breadth (1-10). "
            "Counts technology_roles with >=1 detection above 0.80 confidence. "
            "1=single weak signal. 10=strong signal across all technology_roles."
        ),
    )

    software_type: str
    software_type_ai: str | None = None
    software_type_ai_reasoning: str = ""
    software_type_confidence: float
    software_type_reasoning: str
    specific_identity: str | None = None
    architecture_style: str
    why_this_stack: str
    ecosystem_context: str
    stack_pattern: str
    notable_combinations: list[str]
    missing_patterns: list[str]
    dep_classification_failed: bool = False
    dep_classification_fail_reason: str = "none"
    repair_counters: dict[str, dict[str, int]] = Field(default_factory=dict)
    ai_classification_used: bool
    layer0_prediction: dict | None = None
    software_type_disagreement: dict | None = None

    pattern_matches: list[PatternMatch]
    ai_inferences: list[AiInference]
    confidence_breakdown: dict
    software_type_confidence_original: float | None = None
    ai_calls_made: int
    files_analyzed: int
    patterns_checked: int
    processing_time_ms: int
    flags: list[dict] = []   # quality flags from compute_analysis_flags()
    manifests_selected: list[dict] = []

    @model_validator(mode="after")
    def validate_stack_pattern(self):
        invalid = {
            "library": {"Microservices", "Serverless", "JAMstack"},
            "ml_platform": {"Microservices", "Serverless", "JAMstack"},
            "data_pipeline": {"MVC", "JAMstack", "Microservices"},
        }
        if self.stack_pattern in invalid.get(self.software_type, set()):
            self.stack_pattern = "Custom"
        return self



class RepoData(BaseModel):
    owner: str
    name: str
    full_name: str
    description: str | None = None
    stars: int
    forks: int
    topics: list[str]
    license: str | None = None
    default_branch: str
    created_at: str
    updated_at: str
    file_tree: list[str]
    file_contents: dict[str, str]


class SoftwareTypeAI(BaseModel):
    value: str
    reasoning: str = ""


class SoftwareTypeProvenance(BaseModel):
    pipeline: str
    ai: SoftwareTypeAI


class AnalysisResult(BaseModel):
    analysis_id: str
    repo: RepoData
    stack: StackAnalysis
    software_type: SoftwareTypeProvenance
    repository_classification: RepositoryClassification | None = None


class AnalyzeRequest(BaseModel):
    repo_url: str
    hard_refresh: bool = False


class ExplainabilityReport(BaseModel):
    analysis_id: str
    pattern_matches: list[PatternMatch]
    ai_inferences: list[AiInference]
    software_type_reasoning: str
    specific_identity: str | None = None
    confidence_breakdown: dict
    ai_calls_made: int
    processing_time_ms: int
    patterns_checked: int
    files_analyzed: int


class ChatMessage(BaseModel):
    role: str
    content: str
    timestamp: Optional[str] = None


class ChatSession(BaseModel):
    session_id: str
    analysis_id: str
    messages: list[ChatMessage] = []
    created_at: Optional[str] = None


class ChatRequest(BaseModel):
    analysis_id: str
    session_id: Optional[str] = None
    message: str
    
class InsightsFeedbackRequest(BaseModel):
    quality_score:    Optional[int] = None    # 1-5, None if implicit
    accepted:         bool = True             # False = user replaced it
    edited_fields:    list[str] = []          # which fields were changed
    replacement_text: Optional[dict] = None  # {field: new_text}
    source:           str = "ui"
