from pydantic import BaseModel, Field
from typing import List, Optional

# --- Module G: Aggregation Models ---
class CodeComment(BaseModel):
    file: str = Field(..., description="The relative path to the file being reviewed.")
    position: int = Field(..., description="The exact 1-based diff position of the changed line.")
    severity: str = Field(..., description="Severity level: P0, P1, P2, or P3.")
    role: str = Field(..., description="The subagent role that generated the finding.")
    comment: str = Field(..., description="Constructive code review feedback. Explain what is wrong and provide a concrete solution.")
    confidence: float = Field(1.0, description="Confidence score from 0.0 to 1.0.")
    references_specific_identifier: bool = Field(..., description="True if feedback references a specific function name, variable, or class.")

class CodeReviewResponse(BaseModel):
    comments: List[CodeComment] = Field(default_factory=list)

# --- Module E: Subagent Return Structures ---
class ContextRequest(BaseModel):
    functions: List[str] = Field(default_factory=list, description="Exact names of external functions you need to read.")
    classes: List[str] = Field(default_factory=list, description="Exact names of classes you need to inspect.")
    configs: List[str] = Field(default_factory=list, description="Global configuration variables or settings needed.")
    why: str = Field(..., description="Brief explanation of why this context is required.")

class SubagentFinding(BaseModel):
    position: int = Field(..., description="1-based diff position of the line needing feedback.")
    severity: str = Field(..., description="Severity level: P0, P1, P2, or P3.")
    comment: str = Field(..., description="Critical analysis finding text.")
    confidence: float = Field(..., description="0.0-1.0: your certainty this is a real, reportable issue.")
    references_specific_identifier: bool = Field(..., description="True if a specific variable or class name is referenced.")
    escalated_symbol: Optional[str] = Field(None, description="Provide symbol name if additional file definitions are needed.")

class SubagentResponse(BaseModel):
    status: str = Field(..., description="Return status: 'SUCCESS' if review is completed, or 'NEEDS_CONTEXT' if symbols must be resolved.")
    findings: List[SubagentFinding] = Field(default_factory=list)
    context_request: Optional[ContextRequest] = Field(None, description="Context requested strictly when status is 'NEEDS_CONTEXT'.")