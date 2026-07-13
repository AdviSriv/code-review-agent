from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Literal

# --- Module G: Aggregation Models ---
class CodeComment(BaseModel):
    file: str = Field(..., description="The relative path to the file being reviewed.")
    position: int = Field(..., description="The exact 1-based diff position of the changed line.")
    severity: Literal["P0", "P1", "P2", "P3"] = Field(..., description="Severity level: P0, P1, P2, or P3.")
    role: str = Field(..., description="The subagent role that generated the finding.")
    comment: str = Field(..., description="Constructive code review feedback. Explain what is wrong and provide a concrete solution.")
    confidence: float = Field(1.0, description="Confidence score from 0.0 to 1.0.")
    references_specific_identifier: bool = Field(..., description="True if feedback references a specific function name, variable, or class.")
    
    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data):
        if isinstance(data, dict):
            if "severity" in data and isinstance(data["severity"], str):
                data["severity"] = data["severity"].strip().upper()
        return data

class CodeReviewResponse(BaseModel):
    comments: List[CodeComment] = Field(default_factory=list)

# --- Module E: Subagent Return Structures ---
class ContextRequest(BaseModel):
    functions: List[str] = Field(default_factory=list, description="Exact names of external functions you need to read.")
    classes: List[str] = Field(default_factory=list, description="Exact names of classes you need to inspect.")
    configs: List[str] = Field(default_factory=list, description="Global configuration variables or settings needed.")
    why: str = Field(..., description="Brief explanation of why this context is required.")
    semantic_queries: Optional[List[str]] = Field(default_factory=list, description="Vague concepts or descriptions to query in the vector database.")

class SubagentFinding(BaseModel):
    position: int = Field(..., description="1-based diff position of the line needing feedback.")
    severity: Literal["P0", "P1", "P2", "P3"] = Field(..., description="Severity level: P0, P1, P2, or P3.")
    comment: str = Field(..., description="Critical analysis finding text.")
    confidence: float = Field(..., description="0.0-1.0: your certainty this is a real, reportable issue.")
    references_specific_identifier: bool = Field(..., description="True if a specific variable or class name is referenced.")
    escalated_symbol: Optional[str] = Field(None, description="Provide symbol name if additional file definitions are needed.")
    
    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data):
        if isinstance(data, dict):
            if "severity" in data and isinstance(data["severity"], str):
                data["severity"] = data["severity"].strip().upper()
        return data

class SubagentResponse(BaseModel):
    status: Literal["SUCCESS", "NEEDS_CONTEXT"] = Field(..., description="Return status: 'SUCCESS' if review is completed, or 'NEEDS_CONTEXT' if symbols must be resolved.")
    findings: List[SubagentFinding] = Field(default_factory=list)
    context_request: Optional[ContextRequest] = Field(None, description="Context requested strictly when status is 'NEEDS_CONTEXT'.")
    
    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data):
        if isinstance(data, dict):
            if "status" in data and isinstance(data["status"], str):
                val = data["status"].strip().upper()
                if val in ("SUCCESS", "SUCCESSFUL", "OK", "DONE", "COMPLETE"):
                    data["status"] = "SUCCESS"
                elif "NEEDS" in val or "CONTEXT" in val:
                    data["status"] = "NEEDS_CONTEXT"
        return data

    @model_validator(mode="after")
    def enforce_context_by_status(self) -> 'SubagentResponse':
        if self.status == "SUCCESS":
            self.context_request = None
        return self

class SubagentResponseOllama(BaseModel):
    """
    Ollama-specific schema. Under grammar-constrained decoding (Ollama's 'format' parameter),
    fields are evaluated in exact order. Forcing 'reasoning' first provides the local model
    scratchpad space to perform Chain-of-Thought analysis before committing to a status/finding.
    """
    reasoning: str = Field(..., description="Step-by-step analysis of the diff chunk against your role's focus areas BEFORE deciding status. Reference concrete line numbers/diff positions and explain what you checked and ruled out. This is scratch space, not shown to the user.")
    status: Literal["SUCCESS", "NEEDS_CONTEXT"] = Field(..., description="Return status: 'SUCCESS' if review is completed, or 'NEEDS_CONTEXT' if symbols must be resolved.")
    findings: List[SubagentFinding] = Field(default_factory=list)
    context_request: Optional[ContextRequest] = Field(None, description="Context requested strictly when status is 'NEEDS_CONTEXT'.")
    
    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data):
        if isinstance(data, dict):
            if "status" in data and isinstance(data["status"], str):
                val = data["status"].strip().upper()
                if val in ("SUCCESS", "SUCCESSFUL", "OK", "DONE", "COMPLETE"):
                    data["status"] = "SUCCESS"
                elif "NEEDS" in val or "CONTEXT" in val:
                    data["status"] = "NEEDS_CONTEXT"
        return data

    @model_validator(mode="after")
    def enforce_context_by_status(self) -> 'SubagentResponseOllama':
        if self.status == "SUCCESS":
            self.context_request = None
        return self