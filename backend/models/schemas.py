from pydantic import BaseModel, Field, model_validator
from typing import Optional


class DetectedTech(BaseModel):
    name: str
    confidence: float
    detection_source: str
    version: Optional[str] = None
    category: str
    scope: Optional[str] = None
    origin: Optional[str] = None
    matched_file: Optional[str] = None
    version_spec: Optional[str] = None
    manifest_frequency: Optional[int] = None
    file_count: Optional[int] = None
    emergent_category: Optional[str] = None
    byte_count: Optional[int] = None
    byte_share: Optional[float] = None


class PatternMatch(BaseModel):
    tech: str
    category: str
    matched_file: str
    matched_keyword: str
    confidence: float
    scope: Optional[str] = None
    origin: Optional[str] = None
    version_spec: Optional[str] = None
    manifest_frequency: Optional[int] = None


class AiInference(BaseModel):
    tech: str
    category: str
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
            "Counts categories with >=1 detection above 0.80 confidence. "
            "1=single weak signal. 10=strong signal across all categories."
        ),
    )

    domain: str
    domain_confidence: float
    domain_reasoning: str
    architecture_style: str
    why_this_stack: str
    ecosystem_context: str
    stack_pattern: str
    notable_combinations: list[str]
    missing_patterns: list[str]
    ai_classification_used: bool

    pattern_matches: list[PatternMatch]
    ai_inferences: list[AiInference]
    confidence_breakdown: dict
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
        if self.stack_pattern in invalid.get(self.domain, set()):
            self.stack_pattern = "Custom"
        return self



class RepoData(BaseModel):
    owner: str
    name: str
    full_name: str
    description: Optional[str] = None
    stars: int
    forks: int
    topics: list[str]
    license: Optional[str] = None
    default_branch: str
    created_at: str
    updated_at: str
    file_tree: list[str]
    file_contents: dict[str, str]


class AnalysisResult(BaseModel):
    analysis_id: str
    repo: RepoData
    stack: StackAnalysis


class AnalyzeRequest(BaseModel):
    repo_url: str
    hard_refresh: bool = False


class ExplainabilityReport(BaseModel):
    analysis_id: str
    pattern_matches: list[PatternMatch]
    ai_inferences: list[AiInference]
    domain_reasoning: str
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
